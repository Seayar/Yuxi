import pytest

from yuxi.knowledge.power_spec.tool import _merge_results


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
