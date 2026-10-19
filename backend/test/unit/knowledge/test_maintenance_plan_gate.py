"""处置方案发布门单测：验证 chat_service 维护方案分支的真实行为。

被测对象是生产源码 ``package/yuxi/services/chat_service.py``（相对 ``backend/test`` 定位，
容器内即 ``/app/package/yuxi/services/chat_service.py``）：用 AST 从该源码里提取发布门相关函数，
编译执行后直接断言行为，**不复制实现自证**；固定文案清单从生产有效契约
``yuxi.knowledge.maintenance_plan.contracts`` 正常导入，不替换 ``sys.modules``。

本测试要求完整或集成后的后端源码；缺少源码或契约视为装配失败。
工作区离线补丁检查保留在 out/T11/tests，不随生产测试部署。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from yuxi.knowledge.maintenance_plan import contracts

# 发布门依赖的函数（从生产源码里提取）
GATE_FUNCTIONS = (
    "_message_field",
    "_current_turn_tool_messages",
    "_is_maintenance_plan_context",
    "_resolve_maintenance_plan_output",
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
    assert not missing, f"生产源码缺少发布门函数（补丁未打上？）：{missing}"
    module = ast.Module(body=[found[name] for name in GATE_FUNCTIONS], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict = {"json": json}
    exec(compile(module, str(CHAT_SERVICE_PATH), "exec"), namespace)  # noqa: S102 - 只执行生产源码里提取出的函数
    return namespace


@pytest.fixture(scope="module")
def gate() -> dict:
    """生产 chat_service 里提取出的发布门函数。"""

    return _extract_gate_namespace(CHAT_SERVICE_PATH.read_text(encoding="utf-8"))


def _message(kind: str, content: str = "", **extra) -> dict:
    message = {"type": kind, "content": content}
    message.update(extra)
    return message


def _tool_payload(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


class _Context:
    """最小上下文桩：发布门只读 ``skills``。"""

    def __init__(self, skills):
        self.skills = skills


# ---------------------------------------------------------------------------
# 按 skill 判定
# ---------------------------------------------------------------------------
def test_is_maintenance_plan_context_matches_skill(gate: dict) -> None:
    """含 ``maintenance-plan`` 技能才进本发布门。"""

    is_maintenance_plan = gate["_is_maintenance_plan_context"]
    assert is_maintenance_plan(_Context(["knowledge-base", "maintenance-plan"])) is True
    assert is_maintenance_plan(_Context(["maintenance-plan"])) is True
    assert is_maintenance_plan(_Context(["knowledge-base", "fault-trace"])) is False
    assert is_maintenance_plan(_Context([])) is False
    assert is_maintenance_plan(_Context(None)) is False
    assert is_maintenance_plan(object()) is False


# ---------------------------------------------------------------------------
# 本轮工具结果：只有校验通过的方案才覆盖模型内容
# ---------------------------------------------------------------------------
def test_verified_report_overrides_model(gate: dict) -> None:
    """本轮工具返回校验通过的处置方案报告 → 用其 ``answer_markdown`` 覆盖模型最终消息。"""

    messages = [
        _message("human", "更换#3开闭所3k18断路器"),
        _message("ai", "我先调用工具"),
        _message(
            "tool",
            _tool_payload(
                status="ok",
                answer_source="maintenance_plan",
                answer_verified=True,
                answer_markdown="# 方案卡片",
            ),
        ),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型自己改写的一段话") == "# 方案卡片"


def test_latest_current_turn_tool_message_wins(gate: dict) -> None:
    """本轮有多个校验通过的工具结果时取最后一个。"""

    messages = [
        _message("human", "3k18断路器更换"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 第一次")),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 第二次")),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型内容") == "# 第二次"


def test_json_payload_without_answer_markdown_is_rejected(gate: dict) -> None:
    """本轮工具 JSON 没有 ``answer_markdown``（非报告 JSON）→ 不发布模型补写内容。"""

    messages = [_message("human", "3k18"), _message("tool", _tool_payload(status="ok", report={}))]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型内容") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_ambiguous_request_without_tool_uses_fixed_clarification(gate: dict) -> None:
    """真实 API 中模型可能自行反问；信息全缺时发布门仍统一固定澄清句。"""

    messages = [_message("human", "帮我出个方案"), _message("ai", "您好，请补充设备和任务")]
    assert gate["_resolve_maintenance_plan_output"](messages, "您好，请补充设备和任务") == contracts.CLARIFY_ALL_MESSAGE


def test_ambiguous_current_turn_does_not_reuse_previous_report(gate: dict) -> None:
    """上轮有报告也只按本轮用户输入决定澄清，不能重新发布旧卡片。"""

    messages = [
        _message("human", "更换3k18"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 旧报告")),
        _message("ai", "# 旧报告"),
        _message("human", "帮我出个方案"),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型反问") == contracts.CLARIFY_ALL_MESSAGE


@pytest.mark.parametrize("tool_content", [None, "未校验的方案正文", '"未校验的方案正文"'])
def test_model_plan_cannot_bypass_gate_without_verified_tool(gate: dict, tool_content) -> None:
    """无可信工具报告时不能回退发布模型编写的方案，包括 JSON 字符串旁路。"""

    messages = [_message("human", "更换#3开闭所3k18断路器")]
    if tool_content is not None:
        messages.append(_message("tool", tool_content))
    assert gate["_resolve_maintenance_plan_output"](messages, "# 模型自行生成的方案") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


# ---------------------------------------------------------------------------
# 校验未通过的方案一律不发布（False / 缺失 / 非布尔）
# ---------------------------------------------------------------------------
def test_unverified_report_is_not_published(gate: dict) -> None:
    """``answer_verified=False`` 的方案不得发布，返回校验失败固定文案。"""

    messages = [
        _message("human", "更换3k18"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=False, answer_markdown="未校验方案")),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型回答") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_missing_verification_flag_is_not_published(gate: dict) -> None:
    """``answer_verified`` 缺失的方案不得发布。"""

    messages = [
        _message("human", "更换3k18"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_markdown="未校验方案")),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型回答") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


@pytest.mark.parametrize("verified", ["true", "True", 1, 0, None, ""])
def test_non_boolean_verification_flag_is_not_published(gate: dict, verified) -> None:
    """``answer_verified`` 非布尔（含字符串 ``'true'``）一律不发布。"""

    messages = [
        _message("human", "更换3k18"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=verified, answer_markdown="未校验方案")),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型回答") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_report_without_source_marker_is_not_published(gate: dict) -> None:
    """带 Markdown 但没有 ``answer_source=maintenance_plan`` 标识的载荷不发布。"""

    messages = [
        _message("human", "更换3k18"),
        _message("tool", _tool_payload(answer_verified=True, answer_markdown="来源不明的方案")),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型回答") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_unverified_report_from_previous_turn_does_not_affect_current_turn(gate: dict) -> None:
    """上一轮的未校验报告不影响本轮：本轮没有可信工具结果时返回失败文案。"""

    messages = [
        _message("human", "上一轮：更换#3开闭所3k18断路器"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=False, answer_markdown="上一轮未校验方案")),
        _message("ai", "上一轮的回答"),
        _message("human", "本轮：992SVG开关检修"),
        _message("ai", "本轮模型内容"),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "本轮模型内容") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_unverified_report_is_not_used_with_run_tool_call_ids(gate: dict) -> None:
    """按运行归属取本轮时，未校验报告同样不被发布。"""

    messages = [
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 旧卡片"), tool_call_id="old-1"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=False, answer_markdown="未校验方案"), tool_call_id="new-1"),
    ]
    resolved = gate["_resolve_maintenance_plan_output"](messages, "模型内容", run_tool_call_ids={"new-1"})
    assert resolved == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


# ---------------------------------------------------------------------------
# 固定文案原样透传（清单来自当前生产契约，不硬编码）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("plain_message", sorted(contracts.PLAIN_TOOL_MESSAGES))
def test_each_plain_tool_message_is_passed_through(gate: dict, plain_message: str) -> None:
    """工具返回固定文案 → 原样透传，不被模型内容替换。"""

    messages = [_message("human", "帮我出个方案"), _message("tool", plain_message)]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型补充的推测") == plain_message


def test_plain_message_set_matches_contract() -> None:
    """校验失败文案已纳入契约的固定文案集合（发布门据此原样透传）。"""

    assert contracts.PLAN_VERIFICATION_FAILED_MESSAGE in contracts.PLAIN_TOOL_MESSAGES
    assert contracts.CHOICE_ABORTED_MESSAGE in contracts.PLAIN_TOOL_MESSAGES
    assert contracts.PLAN_GRAPH_NOT_CONFIGURED_MESSAGE in contracts.PLAIN_TOOL_MESSAGES
    assert len(contracts.PLAIN_TOOL_MESSAGES) == len(set(contracts.PLAIN_TOOL_MESSAGES))


def test_other_plain_text_is_rejected(gate: dict) -> None:
    """工具返回其他纯文本 → 返回失败文案。"""

    messages = [_message("human", "1号主变"), _message("tool", "这句不在固定文案里")]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型内容") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_fault_trace_plain_message_is_not_accepted(gate: dict) -> None:
    """故障溯源的固定文案不在本模块白名单 → 返回失败文案（证明取的是本模块契约）。"""

    messages = [_message("human", "3k18"), _message("tool", "故障溯源知识库未配置，无法进行故障溯源")]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型内容") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


# ---------------------------------------------------------------------------
# 只认本轮：上一轮的工具结果不被采用
# ---------------------------------------------------------------------------
def test_previous_turn_tool_card_is_not_used(gate: dict) -> None:
    """上一轮校验通过的工具卡片也不被采用（checkpoint 含线程完整历史）。"""

    messages = [
        _message("human", "上一轮：更换#3开闭所3k18断路器"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 上一轮方案卡片")),
        _message("ai", "上一轮的回答"),
        _message("human", "本轮：992SVG开关检修"),
        _message("ai", "本轮模型内容"),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "本轮模型内容") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_previous_turn_plain_message_is_not_used(gate: dict) -> None:
    """上一轮的固定澄清文案也不被采用。"""

    messages = [
        _message("human", "上一轮问题"),
        _message("tool", contracts.CLARIFY_ALL_MESSAGE),
        _message("human", "本轮：992SVG开关检修"),
        _message("ai", "本轮模型内容"),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "本轮模型内容") == contracts.PLAN_VERIFICATION_FAILED_MESSAGE


def test_run_tool_call_ids_limits_to_current_run(gate: dict) -> None:
    """有运行归属时按 ``tool_call_id`` 取本轮工具结果。"""

    messages = [
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 旧卡片"), tool_call_id="old-1"),
        _message("tool", _tool_payload(answer_source="maintenance_plan", answer_verified=True, answer_markdown="# 新卡片"), tool_call_id="new-1"),
    ]
    assert gate["_resolve_maintenance_plan_output"](messages, "模型内容", run_tool_call_ids={"new-1"}) == "# 新卡片"
