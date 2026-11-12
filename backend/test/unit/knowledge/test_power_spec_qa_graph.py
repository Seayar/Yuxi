"""电力规范测评图谱：命中、条款解析与配置。"""

import json

import pytest

from yuxi.knowledge.power_spec import qa_graph
from yuxi.knowledge.power_spec.contracts import PowerSpecQuery
from yuxi.knowledge.power_spec.intent import parse_power_spec_query
from yuxi.knowledge.power_spec.qa_graph import (
    PowerSpecGraphCitation,
    PowerSpecGraphHit,
    find_power_spec_qa_hit,
    match_question_rows,
    resolve_hit_evidence,
    resolve_power_spec_graph_config,
)

QUESTION = "110kV主变压器停电后，验电前应确认验电器处于什么状态？"
NOG = "在非氧化性气体（NOG）中，10kV高压开关柜裸铜或裸铜合金触头的最高温度和允许温升分别是多少？"
OG = "在氧化性气体（OG）中，10kV高压开关柜裸铜触头的最高温度和允许温升分别是多少？"


def _question_row(qid: str, text: str, *, group: int = 0) -> dict:
    return {
        "qid": qid,
        "qtext": text,
        "qkeys": json.dumps(
            [{"key": qa_graph.normalize_question_text(text), "text": text, "group": group, "type": "标准书面"}],
            ensure_ascii=False,
        ),
        "qbehavior": "READY_FOR_E2E",
        "qsource": "md" if qid.startswith("md:") else "xlsx",
        "qno": qid.split(":")[-1],
    }


class _FakeClient:
    def __init__(self, _config):
        self.closed = False

    def run(self, cypher, **params):
        if "MATCH (q:问题)" in cypher:
            return [_question_row("md:Q21", QUESTION)]
        if "有答案" in cypher:
            return [{"aid": "md:Q21#A", "arefuse": "false", "across": "false", "atext": "直接验电时应在接地处逐相验电。"}]
        if "依据条款" in cypher:
            return [
                {
                    "cstd": "DL/T 408-2023",
                    "cno": "6.3.2",
                    "ctitle": "验电",
                    "ctable": "",
                    "cpage": "7",
                    "cpdf": "13",
                    "csection": "第6.3.2条",
                    "cstatus": "现行",
                    "cexcerpt": "验电前应先在相应电压等级的有电设备上确证验电器良好",
                    "crank": 1,
                    "cgroup": 0,
                }
            ]
        return []

    def close(self):
        self.closed = True


def _document(*, standard_no: str = "DL/T 408-2023", status: str = "废止", clauses=None) -> dict:
    return {
        "standard_no": standard_no,
        "standard_name": "电力安全工作规程 发电厂和变电站电气部分",
        "version": "2023",
        "status": status,
        "source_file": "DLT408-2023.md",
        "clauses": clauses or [],
    }


def _clause(no: str, title: str, body: str) -> dict:
    return {"clause_no": no, "clause_title": title, "clause_body": body}


def test_fuzzy_match_is_blocked_by_standard_guard():
    """规范化会剥掉标准号里的 ``/`` 和 ``-``：guard 必须基于原文比较，仅标准号不同的题不得模糊命中。"""

    row_a = _question_row("md:Q596", "DL/T 596-2021 中，变压器有载分接开关的例行试验周期是多久？")
    near_b = "DL/T 597-2021 中，变压器有载分接开关的例行试验周期是多久？"

    assert match_question_rows([row_a], [near_b]) is None


def test_exact_match_prefers_canonical_question():
    rows = [
        _question_row("md:Q21", QUESTION),
        _question_row("md:Q99", "35kV母线停电后，装设接地线前为什么必须先验明无电压？"),
    ]

    matched = match_question_rows(rows, [QUESTION + "？"])

    assert matched is not None
    assert matched[0].question_id == "md:Q21"
    assert matched[2] == "exact"
    assert matched[3] == 1.0


def test_fuzzy_match_is_blocked_by_negation_guard():
    rows = [_question_row("xlsx:Q38", OG)]

    assert match_question_rows(rows, [NOG]) is None


def test_fuzzy_match_is_blocked_by_voltage_guard():
    rows = [_question_row("xlsx:SINGLE", "35kV高压设备不停电时的最小安全距离是多少？")]

    assert match_question_rows(rows, ["110kV高压设备不停电时的最小安全距离是多少？"]) is None


def test_fuzzy_match_accepts_minor_wording_change():
    rows = [_question_row("md:Q21", QUESTION)]

    matched = match_question_rows(rows, ["110kV主变压器停电后，验电前应确认验电器处于何种状态？"])

    assert matched is not None
    assert matched[2] == "fuzzy"
    assert matched[3] >= qa_graph.POWER_SPEC_QA_GRAPH_MATCH_THRESHOLD


def test_find_hit_reads_question_answer_and_clause(monkeypatch):
    monkeypatch.setattr("yuxi.agents.toolkits.tugraph.TuGraphClient", _FakeClient)

    hit = find_power_spec_qa_hit(
        type("Config", (), {"uri": "bolt://x", "graph": "g", "username": "u", "password": "p"})(),
        parse_power_spec_query(QUESTION),
        original_query_text=QUESTION,
    )

    assert hit is not None
    assert hit.question_id == "md:Q21"
    assert hit.match_kind == "exact"
    assert hit.refusal is False
    assert hit.answer_text == "直接验电时应在接地处逐相验电。"
    assert hit.cross_source is False
    assert [citation.clause_no for citation in hit.citations] == ["6.3.2"]


def test_find_hit_marks_refusal(monkeypatch):
    class RefusalClient(_FakeClient):
        def run(self, cypher, **params):
            if "MATCH (q:问题)" in cypher:
                return [_question_row("xlsx:NEG_01", "锂电池消防喷淋安装距离是多少？")]
            if "有答案" in cypher:
                return [{"aid": "xlsx:NEG_01#A", "arefuse": "true", "across": "false"}]
            return []

    monkeypatch.setattr("yuxi.agents.toolkits.tugraph.TuGraphClient", RefusalClient)

    hit = find_power_spec_qa_hit(
        type("Config", (), {"uri": "bolt://x", "graph": "g", "username": "u", "password": "p"})(),
        parse_power_spec_query("锂电池消防喷淋安装距离是多少？"),
        original_query_text="锂电池消防喷淋安装距离是多少？",
    )

    assert hit is not None
    assert hit.refusal is True
    assert hit.citations == ()


def test_find_hit_reads_cross_source_flag(monkeypatch):
    class CrossSourceClient(_FakeClient):
        def run(self, cypher, **params):
            if "MATCH (q:问题)" in cypher:
                return [_question_row("md:Q40", "110kV 1号主变额定容量和温升限值是多少？")]
            if "有答案" in cypher:
                return [
                    {
                        "aid": "md:Q40#A",
                        "arefuse": "false",
                        "across": "true",
                        "atext": "实际容量需查设备台账或铭牌。",
                    }
                ]
            return []

    monkeypatch.setattr("yuxi.agents.toolkits.tugraph.TuGraphClient", CrossSourceClient)

    hit = find_power_spec_qa_hit(
        type("Config", (), {"uri": "bolt://x", "graph": "g", "username": "u", "password": "p"})(),
        parse_power_spec_query("110kV 1号主变额定容量和温升限值是多少？"),
        original_query_text="110kV 1号主变额定容量和温升限值是多少？",
    )

    assert hit is not None
    assert hit.cross_source is True
    assert hit.answer_text == "实际容量需查设备台账或铭牌。"


def test_corrective_refusal_is_distinguished_from_out_of_scope():
    """错误前提题在图上也是拒答，但运行时必须继续检索并纠正。"""

    def _hit(behavior: str) -> PowerSpecGraphHit:
        return PowerSpecGraphHit(
            question_id="xlsx:NEG_05",
            question="10kV 不停电作业距离只要 0.1 米，对吗？",
            matched_variant="10kV 不停电作业距离只要 0.1 米，对吗？",
            match_kind="exact",
            score=1.0,
            expected_behavior=behavior,
            refusal=True,
            matched_group=0,
        )

    assert _hit("应指出事实前提错误并纠错").corrective is True
    assert _hit("应严肃拦截危险违章操作").corrective is True
    assert _hit("应该如实拒答 (未包含信息)").corrective is False
    assert _hit("拒绝回答 / 声明规程未涵盖").corrective is False


def test_resolve_hit_evidence_uses_graph_status_and_prefix():
    metadata = {
        "file-1": _document(
            clauses=[
                _clause("5.3.1.3", "安全距离", "表1 高压设备不停电时的安全距离：10kV 0.70m；35kV 1.00m；110kV 1.50m。"),
                _clause("6.1.1", "巡检项目", "油浸式电力变压器和电抗器巡检项目及要求如下：本体检查、套管检查等。"),
                _clause("6.1.1.2", "本体检查", "本体检查：基础无位移、沉降，设备标识齐全，无异常声响及振动。"),
            ]
        )
    }
    hit = PowerSpecGraphHit(
        question_id="md:Q01",
        question="主变巡视检查什么",
        matched_variant="主变巡视检查什么",
        match_kind="exact",
        score=1.0,
        expected_behavior=None,
        refusal=False,
        matched_group=0,
        citations=(
            PowerSpecGraphCitation(standard_no="DL/T 408-2023", clause_no="5.3.1.3", rank=1),
            PowerSpecGraphCitation(standard_no="DL/T 408-2023", clause_no="6.1.1", rank=2),
        ),
    )

    resolution = resolve_hit_evidence(hit, metadata)

    assert [item.metadata["graph_rank"] for item in resolution.evidence] == [1, 2, 2]
    assert resolution.evidence[0].status == "现行"
    assert resolution.evidence[0].metadata["qa_graph_hit"] is True
    assert resolution.evidence[1].citation_key == "file-1#6.1.1"
    assert resolution.evidence[2].citation_key == "file-1#6.1.1.2"


def test_table_citation_prefers_actual_table_title_over_cross_reference():
    """“表1”正文引用不能抢在真正的表1标题前面。"""

    metadata = {
        "file-1": _document(
            status="现行",
            clauses=[
                _clause("5.3.1.3", "表1高压设备不停电时的安全距离", "| 10 | 0.70 | 35 | 1.00 |"),
                _clause("14.2.1", "地电位带电作业", "10kV 数据符合 GB/T 18857—2019 的表1。"),
            ],
        )
    }
    hit = PowerSpecGraphHit(
        question_id="xlsx:MULTI_04",
        question="10kV 有遮蔽与 35kV 无遮蔽的安全距离相差多少",
        matched_variant="10kV 有遮蔽与 35kV 无遮蔽的安全距离相差多少",
        match_kind="exact",
        score=1.0,
        expected_behavior=None,
        refusal=False,
        matched_group=0,
        citations=(
            PowerSpecGraphCitation(standard_no="DL/T 408-2023", clause_no="5.3.1.3", table_no="1", rank=1),
        ),
    )

    resolution = resolve_hit_evidence(hit, metadata)

    assert resolution.evidence[0].citation_key == "file-1#5.3.1.3"


def test_resolve_hit_evidence_falls_back_to_table_number():
    metadata = {
        "file-2": _document(
            standard_no="DL/T 596-2021",
            status="现行",
            clauses=[
                _clause("13.2", "电缆试验", "表44 序号2：外护套绝缘电阻每千米不小于0.5MΩ。"),
            ],
        )
    }
    hit = PowerSpecGraphHit(
        question_id="xlsx:MULTI_13",
        question="电缆外护套",
        matched_variant="电缆外护套",
        match_kind="exact",
        score=1.0,
        expected_behavior=None,
        refusal=False,
        matched_group=0,
        citations=(PowerSpecGraphCitation(standard_no="DL/T 596-2021", clause_no="表44", table_no="44", rank=1),),
    )

    resolution = resolve_hit_evidence(hit, metadata)

    assert [item.clause_no for item in resolution.evidence] == ["13.2"]


def test_resolve_hit_evidence_falls_back_to_excerpt():
    excerpt = "保全线有灵敏系数的零序电流定值对本线路末端金属性接地故障的灵敏系数要求"
    metadata = {
        "file-3": _document(
            standard_no="DL/T 584-2017",
            status="现行",
            clauses=[
                _clause(
                    "7.2.1.3",
                    "零序电流保护",
                    f"表1 110kV 线路零序电流保护整定表。{excerpt}为：20km以下不小于1.5。",
                ),
                _clause("7.2.3.6", "相间距离保护", "相间距离Ⅱ段保护对本线路末端相间金属性故障的灵敏系数要求。"),
            ],
        )
    }
    hit = PowerSpecGraphHit(
        question_id="xlsx:SINGLE_12",
        question="零序电流灵敏系数",
        matched_variant="零序电流灵敏系数",
        match_kind="exact",
        score=1.0,
        expected_behavior=None,
        refusal=False,
        matched_group=0,
        citations=(
            PowerSpecGraphCitation(
                standard_no="DL/T 584-2017",
                clause_no="7.2.1.10",
                excerpt=excerpt,
                rank=1,
            ),
        ),
    )

    resolution = resolve_hit_evidence(hit, metadata)

    assert [item.clause_no for item in resolution.evidence] == ["7.2.1.3"]
    assert resolution.unmatched == []


def test_resolve_hit_evidence_reports_missing_clause():
    hit = PowerSpecGraphHit(
        question_id="xlsx:SINGLE",
        question="不存在",
        matched_variant="不存在",
        match_kind="exact",
        score=1.0,
        expected_behavior=None,
        refusal=False,
        matched_group=0,
        citations=(PowerSpecGraphCitation(standard_no="DL/T 408-2023", clause_no="99.99", rank=1),),
    )

    resolution = resolve_hit_evidence(hit, {"file-1": _document(clauses=[])})

    assert resolution.evidence == []
    assert len(resolution.unmatched) == 1


@pytest.mark.asyncio
async def test_resolve_graph_config_reads_admin_option(monkeypatch):
    class FakeOption:
        async def get(self, db=None):
            return {
                "server_url": "bolt://tugraph:7688",
                "graph": "power_spec_qa",
                "username": "admin",
                "password": "secret",
            }

    monkeypatch.setattr(qa_graph, "power_spec_graph_opts", FakeOption())
    monkeypatch.setattr(qa_graph, "POWER_SPEC_QA_GRAPH_ENABLED", True)

    config = await resolve_power_spec_graph_config()

    assert config is not None
    assert (config.uri, config.graph, config.username, config.password) == (
        "bolt://tugraph:7688",
        "power_spec_qa",
        "admin",
        "secret",
    )


@pytest.mark.asyncio
async def test_resolve_graph_config_rejects_incomplete_option(monkeypatch):
    class FakeOption:
        async def get(self, db=None):
            return {"server_url": "bolt://tugraph:7688", "graph": "power_spec_qa"}

    monkeypatch.setattr(qa_graph, "power_spec_graph_opts", FakeOption())
    monkeypatch.setattr(qa_graph, "POWER_SPEC_QA_GRAPH_ENABLED", True)

    assert await resolve_power_spec_graph_config() is None


def test_query_type_is_public_and_can_be_built_directly():
    query = PowerSpecQuery(query="问题", intent="其他类型")

    assert query.query == "问题"
