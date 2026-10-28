"""故障溯源触发条件求值与原因聚合（三值逻辑）。

触发条件 ``表达式`` 使用受限 AST：标识符、数字/布尔字面量、比较、``and``/``or``/
``not``、括号与四则运算；不支持函数调用、属性访问或下标，不使用 ``eval``。
任一必需属性缺失/非法/服务不可用 → ``unknown``，不把缺值当作 ``false``。
"""

from __future__ import annotations

import ast
from typing import Any

from yuxi.knowledge.fault_trace.contracts import (
    ENTITY_CAUSE,
    IMAGE_ATTRIBUTE_TYPES,
    REALTIME_UNAVAILABLE_STATUSES,
    REL_TRIGGER_TYPE,
    CauseEvaluation,
    RealtimeValue,
    TriggerResult,
)
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence

MAX_EXPRESSION_LENGTH = 500
_TRUE = "true"
_FALSE = "false"
_UNKNOWN = "unknown"
# kg3 的表达式使用 JSON 风格小写字面量，Python AST 会解析成 Name。
_LITERAL_NAMES = {"true": True, "false": False}
_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def evaluate_expression(trigger: dict[str, Any], values: dict[str, RealtimeValue]) -> TriggerResult:
    """求值单个触发条件；无表达式时作为定性条件返回 unknown。"""

    trigger_id = str(trigger.get("id") or "")
    name = str(trigger.get("name") or "")
    properties = trigger.get("properties") or {}
    expression = str(properties.get("表达式") or "").strip() or None
    if expression is None:
        return TriggerResult(
            trigger_id=trigger_id,
            name=name,
            expression=None,
            result=_UNKNOWN,
            reason="qualitative_condition",
            qualitative=True,
        )
    if len(expression) > MAX_EXPRESSION_LENGTH:
        return _invalid(trigger_id, name, expression, "expression_invalid")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return _invalid(trigger_id, name, expression, "expression_invalid")
    if not _allowed_nodes(tree):
        return _invalid(trigger_id, name, expression, "expression_invalid")

    attributes = _referenced_attributes(tree)

    missing = _missing_attributes(attributes, values)
    if missing:
        reason = _missing_reason(attributes, values)
        available = {
            attribute: values[attribute].value
            for attribute in attributes
            if attribute in values and values[attribute].ok
        }
        return TriggerResult(
            trigger_id=trigger_id,
            name=name,
            expression=expression,
            result=_UNKNOWN,
            reason=reason,
            values=available,
        )

    try:
        result = _evaluate(tree.body, {attribute: values[attribute].value for attribute in attributes})
    except (ValueError, TypeError):
        return _invalid(trigger_id, name, expression, "expression_invalid")
    if not isinstance(result, bool):
        return _invalid(trigger_id, name, expression, "expression_invalid")
    return TriggerResult(
        trigger_id=trigger_id,
        name=name,
        expression=expression,
        result=_TRUE if result else _FALSE,
        reason="ok",
        values={attribute: values[attribute].value for attribute in attributes},
    )


def evaluate_causes(
    evidence: FaultGraphEvidence,
    values: dict[str, RealtimeValue],
) -> list[CauseEvaluation]:
    """按 ``全部触发条件成立 → established``（AND）聚合每个故障原因。"""

    trigger_by_cause: dict[str, list[dict[str, Any]]] = {}
    for edge in evidence.edges_with_type(REL_TRIGGER_TYPE):
        trigger = evidence.node_by_id(str(edge.get("target_id") or ""))
        if trigger is not None:
            trigger_by_cause.setdefault(str(edge.get("source_id")), []).append(trigger)

    evaluations: list[CauseEvaluation] = []
    for cause in evidence.nodes_with_label(ENTITY_CAUSE):
        cause_id = str(cause.get("id"))
        results = [evaluate_expression(trigger, values) for trigger in trigger_by_cause.get(cause_id, [])]
        evaluable = [result for result in results if not result.qualitative]
        true_count = sum(1 for result in evaluable if result.result == _TRUE)
        false_count = sum(1 for result in evaluable if result.result == _FALSE)
        unknown_count = sum(1 for result in evaluable if result.result == _UNKNOWN)
        if not evaluable:
            status = "unknown"
        elif false_count:
            # AND 语义：任一判据不成立，原因即被排除。
            status = "rejected"
        elif unknown_count:
            status = "unknown"
        else:
            status = "established"
        missing = sorted(set(_missing_attributes_for(results)))
        evaluations.append(
            CauseEvaluation(
                cause_id=cause_id,
                name=str(cause.get("name") or ""),
                status=status,
                triggers=results,
                true_count=true_count,
                missing=missing,
                qualitative_only=not evaluable and bool(results),
            )
        )
    return evaluations


def located_causes(causes: list[CauseEvaluation]) -> list[CauseEvaluation]:
    """全部成立原因，按「true 条件数降序、cause_id 升序」排序。"""

    established = [cause for cause in causes if cause.status == "established"]
    return sorted(established, key=lambda cause: (-cause.true_count, cause.cause_id))


def select_located_cause(causes: list[CauseEvaluation]) -> CauseEvaluation | None:
    """多原因成立时的首要原因（用于卡片主字段与证据）。"""

    ranked = located_causes(causes)
    return ranked[0] if ranked else None


def overall_verdict(causes: list[CauseEvaluation]) -> str:
    if any(cause.status == "established" for cause in causes):
        return "established"
    if any(cause.status == "unknown" for cause in causes):
        return "unknown"
    return "none"


def trigger_summary(cause: CauseEvaluation) -> str:
    """给报告/思考步骤的一句话判据摘要。"""

    evaluable = [trigger for trigger in cause.triggers if not trigger.qualitative]
    if not evaluable:
        return f"{len(cause.triggers)} 条定性条件，缺少实时判据"
    hits = sum(1 for trigger in evaluable if trigger.result == _TRUE)
    unknown = sum(1 for trigger in evaluable if trigger.result == _UNKNOWN)
    parts = [f"{hits} 项实时判据命中"]
    if unknown:
        parts.append(f"{unknown} 项缺少数据")
    if not hits and not unknown:
        parts.append("判据均未命中")
    return "；".join(parts)


def missing_attributes(causes: list[CauseEvaluation]) -> list[str]:
    missing: set[str] = set()
    for cause in causes:
        missing.update(cause.missing)
    return sorted(missing)


def referenced_attributes(expression: str) -> list[str]:
    """表达式引用的属性名（不含 true/false）；非法表达式返回空列表。"""

    text = str(expression or "").strip()
    if not text or len(text) > MAX_EXPRESSION_LENGTH:
        return []
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return []
    if not _allowed_nodes(tree):
        return []
    return _referenced_attributes(tree)


def _allowed_nodes(tree: ast.Expression) -> bool:
    """表达式只允许算术/比较/布尔节点，拒绝调用、属性访问和下标。"""

    return all(isinstance(node, _ALLOWED_NODE_TYPES) for node in ast.walk(tree))


def _referenced_attributes(tree: ast.Expression) -> list[str]:
    return list(
        dict.fromkeys(
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in _LITERAL_NAMES
        )
    )


def _missing_attributes(attributes: list[str], values: dict[str, RealtimeValue]) -> list[str]:
    return [attribute for attribute in attributes if attribute not in values or not values[attribute].ok]


def _missing_reason(attributes: list[str], values: dict[str, RealtimeValue]) -> str:
    statuses = [values[attribute].status if attribute in values else "missing" for attribute in attributes]
    if any(attribute in IMAGE_ATTRIBUTE_TYPES for attribute in attributes):
        return "image_required"
    if any(status in REALTIME_UNAVAILABLE_STATUSES for status in statuses):
        return "redis_unavailable"
    if "invalid" in statuses:
        return "invalid_value"
    return "missing_key"


def _missing_attributes_for(results: list[TriggerResult]) -> list[str]:
    """从求值结果反查缺失属性（用于报告 missing_data）。"""

    missing: set[str] = set()
    for result in results:
        if result.result != _UNKNOWN or result.qualitative or not result.expression:
            continue
        try:
            tree = ast.parse(result.expression, mode="eval")
        except SyntaxError:
            continue
        for attribute in _referenced_attributes(tree):
            if attribute not in (result.values or {}):
                missing.add(attribute)
    return sorted(missing)


def _invalid(trigger_id: str, name: str, expression: str, reason: str) -> TriggerResult:
    return TriggerResult(
        trigger_id=trigger_id,
        name=name,
        expression=expression,
        result=_UNKNOWN,
        reason=reason,
    )


def _evaluate(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        results = [_evaluate(value, values) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(bool(result) for result in results)
        return any(bool(result) for result in results)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate(node.operand, values)
        if isinstance(node.op, ast.Not):
            return not bool(operand)
        if isinstance(node.op, ast.USub):
            return -_number(operand)
        if isinstance(node.op, ast.UAdd):
            return _number(operand)
        raise ValueError("unsupported unary operator")
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, values)
            if not _compare(operator, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        left = _number(_evaluate(node.left, values))
        right = _number(_evaluate(node.right, values))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        raise ValueError("unsupported binary operator")
    if isinstance(node, ast.Name):
        if node.id in _LITERAL_NAMES:
            return _LITERAL_NAMES[node.id]
        if node.id not in values:
            raise ValueError("unknown attribute")
        return values[node.id]
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("unsupported literal")
    raise ValueError("unsupported expression node")


def _compare(operator: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(operator, ast.Eq):
        return left == right
    if isinstance(operator, ast.NotEq):
        return left != right
    if isinstance(operator, ast.Gt):
        return _number(left) > _number(right)
    if isinstance(operator, ast.GtE):
        return _number(left) >= _number(right)
    if isinstance(operator, ast.Lt):
        return _number(left) < _number(right)
    if isinstance(operator, ast.LtE):
        return _number(left) <= _number(right)
    raise ValueError("unsupported comparison")


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError("expected number")
