"""故障溯源发布门单测：验证 chat_service 故障溯源分支的真实行为。

被测对象是生产源码 ``package/yuxi/services/chat_service.py``：用 AST 从该源码里提取发布门
相关函数，编译执行后直接断言行为，不复制实现自证；固定文案从生产契约
``yuxi.knowledge.fault_trace.contracts`` 正常导入。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from yuxi.knowledge.fault_trace import contracts

pytestmark = pytest.mark.unit

GATE_FUNCTIONS = (
    "_message_field",
    "_current_turn_tool_messages",
    "_resolve_fault_trace_output",
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


@pytest.fixture(scope="module")
def gate() -> dict:
    """生产 chat_service 里提取出的发布门函数。"""

    source = CHAT_SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in GATE_FUNCTIONS}
    missing = sorted(set(GATE_FUNCTIONS) - set(found))
    assert not missing, f"生产源码缺少发布门函数：{missing}"
    module = ast.Module(body=[found[name] for name in GATE_FUNCTIONS], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict = {"json": json}
    exec(compile(module, str(CHAT_SERVICE_PATH), "exec"), namespace)  # noqa: S102 - 只执行生产源码里提取出的函数
    return namespace


def _message(kind: str, content: str = "", **extra) -> dict:
    message = {"type": kind, "content": content}
    message.update(extra)
    return message


def _report(**payload) -> str:
    return json.dumps(
        {"status": "ok", "answer_source": "fault_trace", "answer_markdown": "# 报告卡片", **payload},
        ensure_ascii=False,
    )


def test_verified_report_overrides_model(gate: dict) -> None:
    """校验通过的溯源报告覆盖模型最终消息。"""

    messages = [
        _message("human", "3B-7Y 通道内树木倒伏"),
        _message("ai", "我先调用工具"),
        _message("tool", _report(answer_verified=True)),
    ]

    assert gate["_resolve_fault_trace_output"](messages, "模型自己改写的一段话") == "# 报告卡片"


@pytest.mark.parametrize(
    "payload",
    [{}, {"answer_verified": False}, {"answer_verified": "true"}, {"answer_verified": 1}],
)
def test_unverified_report_is_not_published(gate: dict, payload: dict) -> None:
    """``answer_verified`` 缺失、为假或非布尔时不得发布报告。"""

    messages = [_message("human", "3B-7Y 通道内树木倒伏"), _message("tool", _report(**payload))]

    assert (
        gate["_resolve_fault_trace_output"](messages, "模型内容")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_report_from_other_source_is_not_published(gate: dict) -> None:
    """来源不是 fault_trace 的载荷即使自带 answer_markdown 也不发布。"""

    messages = [
        _message("human", "3B-7Y 通道内树木倒伏"),
        _message(
            "tool",
            json.dumps(
                {"answer_source": "power_spec", "answer_verified": True, "answer_markdown": "# 别的来源"},
                ensure_ascii=False,
            ),
        ),
    ]

    assert (
        gate["_resolve_fault_trace_output"](messages, "模型内容")
        == contracts.REPORT_VERIFICATION_FAILED_MESSAGE
    )


def test_plain_fixed_message_passes_through(gate: dict) -> None:
    """契约里的固定文案原样透传。"""

    messages = [_message("human", "3B-7Y"), _message("tool", contracts.DEVICE_NOT_FOUND_MESSAGE)]

    assert gate["_resolve_fault_trace_output"](messages, "模型内容") == contracts.DEVICE_NOT_FOUND_MESSAGE


def test_without_tool_result_model_content_is_kept(gate: dict) -> None:
    """本轮没有工具结果时保留模型内容。"""

    messages = [_message("human", "3B-7Y 通道内树木倒伏"), _message("ai", "模型回答")]

    assert gate["_resolve_fault_trace_output"](messages, "模型回答") == "模型回答"


def test_plain_text_not_in_whitelist_falls_back(gate: dict) -> None:
    """不在白名单里的普通文本不作为发布内容。"""

    messages = [_message("human", "3B-7Y"), _message("tool", "模型自由文本")]

    assert gate["_resolve_fault_trace_output"](messages, "回退内容") == "回退内容"


def test_clarification_payload_publishes_fixed_question(gate: dict) -> None:
    """工具真实返回的澄清载荷必须发布固定问句，不回退成模型自由文本。"""

    from yuxi.agents.thinking import ThinkingEmitter
    from yuxi.knowledge.fault_trace.workflow import _clarify

    payload = _clarify(
        ThinkingEmitter(agent_slug=contracts.FAULT_TRACE_AGENT_SLUG, steps=contracts.FAULT_TRACE_THINKING_STEPS),
        query=contracts.FaultTraceQuery(device_name="3B-7Y", phenomena=["通道异常"]),
        fallback=contracts.DEVICE_NOT_FOUND_MESSAGE,
    )
    messages = [
        _message("human", "3B-7Y 通道异常"),
        _message("tool", json.dumps(payload, ensure_ascii=False), tool_call_id="call-1"),
    ]

    assert (
        gate["_resolve_fault_trace_output"](messages, "模型自己编的一段结论")
        == contracts.DEVICE_NOT_FOUND_MESSAGE
    )
