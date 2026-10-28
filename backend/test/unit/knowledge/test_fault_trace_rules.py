"""触发条件求值、原因聚合与定位规则单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.contracts import RealtimeValue
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence
from yuxi.knowledge.fault_trace.rules import (
    evaluate_causes,
    evaluate_expression,
    located_causes,
    overall_verdict,
    referenced_attributes,
    select_located_cause,
    trigger_summary,
)

pytestmark = pytest.mark.unit


def _values(**items) -> dict[str, RealtimeValue]:
    return {name: RealtimeValue(name, "ok", value, raw=str(value)) for name, value in items.items()}


def _trigger(trigger_id: str, expression: str | None) -> dict:
    return {
        "id": trigger_id,
        "name": trigger_id,
        "type": "trigger",
        "properties": {"id": trigger_id, "名称": trigger_id, **({"表达式": expression} if expression else {})},
    }


def test_expression_true_and_false():
    values = _values(leakage_current=1.2, line_protection_trip=False)

    hit = evaluate_expression(_trigger("T1", "leakage_current > 1"), values)
    miss = evaluate_expression(_trigger("T2", "line_protection_trip == true"), values)

    assert (hit.result, hit.reason) == ("true", "ok")
    assert (miss.result, miss.reason) == ("false", "ok")


def test_expression_missing_attribute_is_unknown_not_false():
    values = _values(leakage_current=0.6)

    result = evaluate_expression(_trigger("T1", "foreign_object_temp_rise > 10"), values)

    assert (result.result, result.reason) == ("unknown", "missing_key")


def test_expression_image_attribute_requires_image_result():
    result = evaluate_expression(
        _trigger("T1", "image_tree_fall_detected == true"),
        {"image_tree_fall_detected": RealtimeValue("image_tree_fall_detected", "not_collected")},
    )

    assert (result.result, result.reason) == ("unknown", "image_required")


def test_expression_unavailable_service_and_invalid_syntax_are_unknown():
    unavailable = evaluate_expression(
        _trigger("T1", "leakage_current > 1"),
        {"leakage_current": RealtimeValue("leakage_current", "unavailable")},
    )
    assert (unavailable.result, unavailable.reason) == ("unknown", "redis_unavailable")

    not_configured = evaluate_expression(
        _trigger("T1", "leakage_current > 1"),
        {"leakage_current": RealtimeValue("leakage_current", "not_configured")},
    )
    assert (not_configured.result, not_configured.reason) == ("unknown", "redis_unavailable")

    for expression in ("__import__('os')", "leakage_current +", "1"):
        result = evaluate_expression(_trigger("T1", expression), _values(leakage_current=1.2))
        assert result.result == "unknown"
        assert result.reason == "expression_invalid"

    unknown_attribute = evaluate_expression(
        _trigger("T1", "unknown_attr > 1"), _values(leakage_current=1.2)
    )
    assert (unknown_attribute.result, unknown_attribute.reason) == ("unknown", "missing_key")


def test_expression_supports_boolean_and_arithmetic():
    values = _values(leakage_current=1.2, uv_photon_rate=2600, line_protection_trip=False)

    combined = evaluate_expression(
        _trigger("T1", "leakage_current > 1 and (uv_photon_rate > 1000 or line_protection_trip == true)"),
        values,
    )
    arithmetic = evaluate_expression(_trigger("T2", "leakage_current * 2 > 2"), values)

    assert combined.result == "true"
    assert arithmetic.result == "true"


def test_unregistered_attributes_are_supported():
    values = {"winding_local_temp": RealtimeValue("winding_local_temp", "ok", 180.0, raw="180")}

    result = evaluate_expression(_trigger("T1", "winding_local_temp > 85"), values)

    assert (result.result, result.reason) == ("true", "ok")


def _cause_evidence(causes: list[tuple[str, list[tuple[str, str | None]]]]) -> FaultGraphEvidence:
    nodes = []
    edges = []
    for cause_id, triggers in causes:
        nodes.append({"id": cause_id, "name": cause_id, "type": "cause", "properties": {"label": "cause"}})
        for trigger_id, expression in triggers:
            nodes.append(_trigger(trigger_id, expression))
            edges.append(
                {
                    "id": f"{cause_id}->{trigger_id}",
                    "source_id": cause_id,
                    "target_id": trigger_id,
                    "type": "has_trigger_condition",
                    "properties": {},
                }
            )
    return FaultGraphEvidence(nodes=nodes, edges=edges)


def test_cause_aggregation_requires_all_conditions_and_ignores_qualitative():
    evidence = _cause_evidence(
        [
            ("CA-1", [("T1", "leakage_current > 1"), ("Q1", None)]),
            ("CA-2", [("T2", "leakage_current > 5")]),
            ("CA-3", [("T3", "foreign_object_temp_rise > 10")]),
            ("CA-4", [("Q2", None)]),
            ("CA-5", [("T4", "leakage_current > 1"), ("T5", "leakage_current > 5")]),
        ]
    )
    values = _values(leakage_current=1.2)

    causes = {cause.cause_id: cause for cause in evaluate_causes(evidence, values)}

    assert causes["CA-1"].status == "established"
    assert causes["CA-2"].status == "rejected"
    assert causes["CA-3"].status == "unknown"
    assert causes["CA-4"].status == "unknown"
    assert causes["CA-4"].qualitative_only is True
    # AND 语义：一条成立、一条不成立，原因被排除。
    assert causes["CA-5"].status == "rejected"
    assert causes["CA-5"].true_count == 1


def test_located_cause_prefers_more_true_conditions_then_cause_id():
    evidence = _cause_evidence([("CA-04", [("T1", "leakage_current > 1"), ("T2", "uv_photon_rate > 1000")])])
    evidence.nodes.append({"id": "CA-02", "name": "CA-02", "type": "cause", "properties": {"label": "cause"}})
    evidence.edges.append(
        {"id": "e", "source_id": "CA-02", "target_id": "T1", "type": "has_trigger_condition", "properties": {}}
    )

    causes = evaluate_causes(evidence, _values(leakage_current=1.2, uv_photon_rate=2600))
    located = select_located_cause(causes)

    assert located is not None
    assert located.cause_id == "CA-04"
    assert overall_verdict(causes) == "established"
    assert trigger_summary(located) == "2 项实时判据命中"


def test_located_causes_lists_all_established_sorted():
    evidence = _cause_evidence(
        [
            ("CA-02", [("T1", "leakage_current > 1")]),
            ("CA-04", [("T2", "leakage_current > 1"), ("T3", "uv_photon_rate > 1000")]),
            ("CA-05", [("T4", "leakage_current > 5")]),
        ]
    )

    causes = evaluate_causes(evidence, _values(leakage_current=1.2, uv_photon_rate=2600))
    ranked = located_causes(causes)

    assert [cause.cause_id for cause in ranked] == ["CA-04", "CA-02"]
    assert select_located_cause(causes).cause_id == "CA-04"


def test_overall_verdict_unknown_and_none():
    unknown = evaluate_causes(
        _cause_evidence([("CA-1", [("T1", "leakage_current > 1")])]),
        {"leakage_current": RealtimeValue("leakage_current", "missing")},
    )
    rejected = evaluate_causes(
        _cause_evidence([("CA-1", [("T1", "leakage_current > 1")])]),
        _values(leakage_current=0.2),
    )

    assert overall_verdict(unknown) == "unknown"
    assert select_located_cause(unknown) is None
    assert overall_verdict(rejected) == "none"


def test_referenced_attributes_skips_boolean_keywords():
    assert referenced_attributes("a > 1 and b == true") == ["a", "b"]
    assert referenced_attributes("not c or d < 3") == ["c", "d"]
    assert referenced_attributes("__import__('os')") == []
    assert referenced_attributes("not a valid (") == []
