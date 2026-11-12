"""设备预测维护发布门单测：验证 chat_service 设备预测维护分支的真实行为。

被测对象是生产源码 ``package/yuxi/services/chat_service.py``（相对 ``backend/test`` 定位）：
用 AST 从源码里提取发布门相关函数，编译执行后直接断言行为，不复制实现自证；固定文案清单
从生产契约 ``yuxi.knowledge.device_prediction.contracts`` 正常导入。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from yuxi.knowledge.device_prediction import contracts

pytestmark = pytest.mark.unit

GATE_FUNCTIONS = (
    "_message_field",
    "_current_turn_tool_messages",
    "_is_device_prediction_context",
    "_resolve_device_prediction_output",
    "_resolve_tool_card_answer",
)


def _locate_chat_service() -> Path | None:
    """相对 ``backend/test`` 定位生产源码 ``backend/package/yuxi/services/chat_service.py``。"""

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "package" / "yuxi" / "services" / "chat_service.py"
        if candidate.is_file():
            return candidate
    return None


CHAT_SERVICE_PATH = _locate_chat_service()
assert CHAT_SERVICE_PATH is not None, "缺少真实 chat_service 源码，发布门测试不能运行"


def _extract_gate_namespace(source: str) -> dict:
    """用 AST 提取发布门函数并编译执行，返回其命名空间。"""

    tree = ast.parse(source)
    found = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in GATE_FUNCTIONS}
    missing = sorted(set(GATE_FUNCTIONS) - set(found))
    assert not missing, f"生产源码缺少发布门函数：{missing}"
    module = ast.Module(body=[found[name] for name in GATE_FUNCTIONS], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict = {"json": json}
    exec(compile(module, str(CHAT_SERVICE_PATH), "exec"), namespace)  # noqa: S102 - 只执行生产源码里提取出的函数
    return namespace


@pytest.fixture(scope="module")
def gate() -> dict:
    return _extract_gate_namespace(CHAT_SERVICE_PATH.read_text(encoding="utf-8"))


def _message(kind: str, content: str = "", **extra) -> dict:
    message = {"type": kind, "content": content}
    message.update(extra)
    return message


def _tool_payload(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _report(markdown: str = "# 报告卡片", *, verified=True, source="device_prediction") -> str:
    """本轮工具返回的报告载荷（默认是校验通过的预测报告）。"""

    return _tool_payload(answer_source=source, answer_verified=verified, answer_markdown=markdown)


class _Context:
    """最小上下文桩：发布门只读 ``skills``。"""

    def __init__(self, skills):
        self.skills = skills


# ---------------------------------------------------------------------------
# 按 skill 判定
# ---------------------------------------------------------------------------
def test_is_device_prediction_context_matches_skill(gate: dict) -> None:
    """含 ``device-prediction`` 技能才进本发布门。"""

    is_prediction = gate["_is_device_prediction_context"]
    assert is_prediction(_Context(["knowledge-base", "device-prediction"])) is True
    assert is_prediction(_Context(["device-prediction"])) is True
    assert is_prediction(_Context(["knowledge-base", "maintenance-plan"])) is False
    assert is_prediction(_Context([])) is False
    assert is_prediction(_Context(None)) is False
    assert is_prediction(object()) is False


# ---------------------------------------------------------------------------
# 本轮工具结果：只有校验通过的报告才覆盖模型内容
# ---------------------------------------------------------------------------
def test_verified_report_overrides_model(gate: dict) -> None:
    """本轮工具返回校验通过的预测报告 → 用其 ``answer_markdown`` 覆盖模型最终消息。"""

    messages = [
        _message("human", "评估1号主变的健康状态"),
        _message("ai", "我先调用工具"),
        _message("tool", _report()),
    ]

    assert gate["_resolve_device_prediction_output"](messages, "模型自己改写的一段话") == "# 报告卡片"


def test_latest_report_in_current_turn_wins(gate: dict) -> None:
    messages = [
        _message("human", "评估1号主变"),
        _message("tool", _report("# 第一次")),
        _message("tool", _report("# 第二次")),
    ]

    assert gate["_resolve_device_prediction_output"](messages, "模型内容") == "# 第二次"


@pytest.mark.parametrize("verified", [False, None, "true", 1, 0])
def test_unverified_report_is_not_published(gate: dict, verified) -> None:
    """``answer_verified`` 不是严格布尔 ``True`` 时一律不发布。"""

    messages = [
        _message("human", "评估1号主变"),
        _message("tool", _report("# 未校验", verified=verified)),
    ]

    assert (
        gate["_resolve_device_prediction_output"](messages, "模型内容")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_report_without_source_marker_is_not_published(gate: dict) -> None:
    """带 Markdown 但没有 ``answer_source=device_prediction`` 的载荷不发布。"""

    messages = [
        _message("human", "评估1号主变"),
        _message("tool", _report("# 别的卡片", source="maintenance_plan")),
    ]

    assert (
        gate["_resolve_device_prediction_output"](messages, "模型内容")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_plain_tool_message_is_passed_through(gate: dict) -> None:
    """工具给出的固定文案原样透传，不被模型改写。"""

    for message in sorted(contracts.PLAIN_TOOL_MESSAGES):
        messages = [_message("human", "评估1号主变"), _message("tool", message)]
        assert gate["_resolve_device_prediction_output"](messages, "模型补充的推测") == message


def test_previous_turn_report_is_not_reused(gate: dict) -> None:
    """上一轮的预测报告不能被当作本轮答案。"""

    messages = [
        _message("human", "评估1号主变"),
        _message("tool", _report("# 上一轮")),
        _message("ai", "上一轮回复"),
        _message("human", "评估2号主变"),
        _message("ai", "本轮模型内容"),
    ]

    assert (
        gate["_resolve_device_prediction_output"](messages, "本轮模型内容")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_round_tool_call_ids_scope_report(gate: dict) -> None:
    """按运行归属过滤：本轮没有可信结果时不复用上一轮工具结果。"""

    messages = [
        _message("human", "评估1号主变"),
        _message("tool", _tool_payload(answer_source="device_prediction", answer_verified=True, answer_markdown="# 旧"),
                 tool_call_id="old-1"),
        _message("tool", _tool_payload(answer_source="device_prediction", answer_verified=True, answer_markdown="# 新"),
                 tool_call_id="new-1"),
    ]

    resolved = gate["_resolve_device_prediction_output"](messages, "模型内容", run_tool_call_ids={"new-1"})

    assert resolved == "# 新"


def test_missing_device_question_falls_back_to_clarification(gate: dict) -> None:
    """模型跳过工具直接反问时，仍然按本轮原文给出固定澄清句。"""

    messages = [_message("human", "你好"), _message("ai", "您好，请补充设备")]

    assert gate["_resolve_device_prediction_output"](messages, "您好，请补充设备") == contracts.CLARIFY_DEVICE_MESSAGE


def test_device_question_without_tool_result_fails_closed(gate: dict) -> None:
    """用户已经给出设备但模型没调用工具 → 返回固定失败文案，不回退模型内容。"""

    messages = [_message("human", "评估1号主变的健康状态"), _message("ai", "1号主变一切正常")]

    assert (
        gate["_resolve_device_prediction_output"](messages, "1号主变一切正常")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_unknown_tool_text_is_not_published(gate: dict) -> None:
    """工具返回非白名单纯文本时不发布，也不回退模型内容。"""

    messages = [_message("human", "评估1号主变"), _message("tool", "模型编造的报告正文")]

    assert (
        gate["_resolve_device_prediction_output"](messages, "模型内容")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_json_string_payload_is_not_published(gate: dict) -> None:
    """工具返回 JSON 字符串载荷时只有命中固定文案白名单才发布。"""

    for payload in (json.dumps("模型编造的报告正文", ensure_ascii=False), json.dumps("随便一段话", ensure_ascii=False)):
        messages = [_message("human", "评估1号主变"), _message("tool", payload)]
        assert (
            gate["_resolve_device_prediction_output"](messages, "模型内容")
            == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
        )


def test_json_string_plain_message_is_published(gate: dict) -> None:
    """JSON 字符串形式的固定文案仍原样透传。"""

    message = contracts.HEALTH_SERVICE_UNAVAILABLE_MESSAGE
    messages = [_message("human", "评估1号主变"), _message("tool", json.dumps(message, ensure_ascii=False))]

    assert gate["_resolve_device_prediction_output"](messages, "模型内容") == message


def test_chat_service_wires_device_prediction_gate() -> None:
    """发布门必须真的接在会话消息分支上，而不只是存在同名函数。"""

    source = CHAT_SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_is_device_prediction_context", "_resolve_device_prediction_output"}
    ]
    assert {node.func.id for node in calls} == {
        "_is_device_prediction_context",
        "_resolve_device_prediction_output",
    }, "chat_service 未把设备预测维护发布门接进消息分支"
