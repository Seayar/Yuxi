import pytest

from yuxi.knowledge.power_spec import tool as tool_module
from yuxi.knowledge.power_spec.contracts import PowerSpecQuery
from yuxi.knowledge.power_spec.intent import parse_power_spec_query
from yuxi.knowledge.power_spec.tool import (
    _angle_routes,
    _merge_primary_results,
    _merge_results,
    _needs_angle_expansion,
    _retrieval_routes,
)


def _result(clause_no: str, score: float, *, file_id: str = "f1") -> dict:
    return {
        "rerank_score": score,
        "content": f"{clause_no} 正文",
        "metadata": {"file_id": file_id, "chunk_id": clause_no},
    }


def test_keyword_only_items_never_outrank_the_question_path():
    """关键词路独有的条目不得占在问题路排名之上。

    实测「10kV 开关柜装设接地线应先接哪端」：问题路第一位就是正确条款
    （DL/T 409 6.4.3「应先装接地端，后装导线端」），但关键词路用模型给的泛化词
    「操作顺序」检索带进来的条款分数更高，合并后把正确条款挤出最终名额，生产链路
    最终输出了完全无关的条款。
    """

    base = [_result("6.4.3", 0.52), _result("12.2.2", 0.41)]
    extra = [_result("7.3.3.3", 0.90), _result("7.3.3.1", 0.88)]

    merged = _merge_results(base, extra)
    by_clause = {item["metadata"]["chunk_id"]: item for item in merged}

    # 召回被补充：关键词路独有的条款仍然保留
    assert set(by_clause) == {"6.4.3", "12.2.2", "7.3.3.3", "7.3.3.1"}
    # 问题路条目保持自己的分数
    assert by_clause["6.4.3"]["rerank_score"] == pytest.approx(0.52)
    # 关键词路独有的条目被压到不高于问题路最低分，不得反过来排在问题路之上
    assert by_clause["7.3.3.3"]["rerank_score"] <= 0.41
    assert by_clause["7.3.3.1"]["rerank_score"] <= 0.41
    ranked = sorted(merged, key=lambda item: item["rerank_score"], reverse=True)
    assert [item["metadata"]["chunk_id"] for item in ranked][:2] == ["6.4.3", "12.2.2"]
    assert by_clause["7.3.3.3"]["metadata"]["keyword_only"] is True


def test_base_entry_wins_when_both_paths_return_the_same_chunk():
    """两路都召回同一条款时保留问题路的那份，避免关键词路的虚高分改变排序。"""

    base = [_result("6.4.3", 0.30)]
    extra = [_result("6.4.3", 0.95)]

    merged = _merge_results(base, extra)

    assert len(merged) == 1
    assert merged[0]["rerank_score"] == pytest.approx(0.30)
    assert not merged[0]["metadata"].get("keyword_only")


def test_retrieval_routes_keep_rewrite_and_original_question():
    """路线顺序固定为改写问题、原始问题；负向：显式“问题1./问题2.”不再拆成独立路线。"""

    query = PowerSpecQuery(query="改写后的问题", intent="其他类型")
    routes = _retrieval_routes("问题1. 子问题一的具体内容是什么？ 问题2. 子问题二的具体内容是什么？", query)

    assert routes == [
        "改写后的问题",
        "问题1. 子问题一的具体内容是什么？ 问题2. 子问题二的具体内容是什么？",
    ]


def test_merge_primary_results_keeps_max_score_and_counts_route_hits():
    """同一 chunk 被多路召回时取最高分，并记录命中路线数供排序加权。"""

    merged = _merge_primary_results(
        [
            [_result("A", 0.5), _result("B", 0.4)],
            [_result("A", 0.7), _result("C", 0.3)],
        ]
    )
    by_clause = {item["metadata"]["chunk_id"]: item for item in merged}

    assert set(by_clause) == {"A", "B", "C"}
    assert by_clause["A"]["rerank_score"] == pytest.approx(0.7)
    assert by_clause["A"]["metadata"]["route_hits"] == 2
    assert by_clause["B"]["metadata"]["route_hits"] == 1


def test_broad_question_expands_to_device_angles():
    """宽问题按设备与电压拆成安装、运行、检修试验、保护、冷却/油/接地角度。"""

    text = "110kV电力变压器有哪些注意事项"
    query = parse_power_spec_query(text)

    assert _needs_angle_expansion(text, query) is True
    angles = _angle_routes(query)

    assert len(angles) == 5
    assert angles[0] == "110kV 变压器 安装与交接试验"
    assert all(angle.startswith("110kV 变压器 ") for angle in angles)
    assert any("保护与监测" in angle for angle in angles)


def test_broad_question_routes_include_angles():
    """宽问题的检索路线包含角度查询，而不是只发一个泛查询。"""

    text = "110kV电力变压器有哪些注意事项"
    query = parse_power_spec_query(text)
    routes = _retrieval_routes(text, query)

    assert len(routes) == 6
    assert any("安装与交接试验" in route for route in routes)
    assert any("冷却、绝缘油与接地" in route for route in routes)


def test_specific_numeric_question_does_not_expand_angles():
    """负向：具体数值问题不按角度拆分，避免无谓的召回与延迟。"""

    text = "110kV变压器温升限值是多少"
    query = parse_power_spec_query(text)

    assert _needs_angle_expansion(text, query) is False
    assert _retrieval_routes(text, query) == [query.query]


def test_broad_question_without_device_does_not_expand_angles():
    """负向：没有设备类型时无法定向扩展，不能凭空拼角度查询。"""

    text = "有哪些注意事项"
    query = parse_power_spec_query(text)

    assert _needs_angle_expansion(text, query) is False
    assert _angle_routes(query) == []


def test_angle_routes_are_capped_by_configuration(monkeypatch):
    """角度数量由 POWER_SPEC_ANGLE_ROUTES 控制，避免每个宽问题都发满所有角度。"""

    query = parse_power_spec_query("110kV电力变压器有哪些注意事项")
    monkeypatch.setattr(tool_module, "POWER_SPEC_ANGLE_ROUTES", 2)

    assert len(_angle_routes(query)) == 2
