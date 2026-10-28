"""chat_service 对智能体思考事件的通用校验。"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.knowledge.power_spec import contracts as _power_spec_contracts  # noqa: F401 - 导入即注册四步思考
from yuxi.services import chat_service
from yuxi.services import conversation_service
from yuxi.services.subagent_run_service import serialize_subagent_run_state


def _context():
    return SimpleNamespace(agent_slug="power-spec-agent")


def test_accepts_known_step_and_overrides_title():
    event = chat_service._accept_thinking_payload(
        {
            "type": "yuxi.thinking_step",
            "thinking": {
                "agent": "power_spec",
                "step_id": "parse_query",
                "step_index": 1,
                "title": "伪造标题",
                "state": "completed",
                "content": "完成",
            },
        },
        _context(),
    )

    assert event is not None
    assert event["title"] == "解析自然语言查询"
    assert event["step_index"] == 1


def test_history_metadata_keeps_thinking_steps_for_audit_backed_messages():
    message = SimpleNamespace(
        extra_metadata={
            "thinking_steps": [{"step_id": "parse_query"}],
            "internal_model": "hidden",
        },
        operation_id="op-1",
    )

    metadata = conversation_service._serialize_history_metadata(message)

    assert metadata["thinking_steps"] == [{"step_id": "parse_query"}]
    assert "internal_model" not in metadata


def test_detects_internal_model_metadata():
    assert chat_service._is_internal_model_metadata({"yuxi_internal_model": True}) is True
    assert chat_service._is_internal_model_metadata({"metadata": {"yuxi_internal_model": True}}) is True
    assert chat_service._is_internal_model_metadata({"tags": ["yuxi_internal_model"]}) is True
    assert chat_service._is_internal_model_metadata({"langgraph_node": "tools"}) is True
    assert chat_service._is_internal_model_metadata({"langgraph_node": "agent"}) is False


def test_subagent_internal_flag_is_serialized():
    run = SimpleNamespace(
        id="child-run",
        agent_slug="power-spec-organizer",
        conversation_thread_id="child-thread",
        status="completed",
        created_at=None,
        finished_at=None,
        error_message=None,
        input_payload={
            "runtime": {
                "tool_call_id": "tool-1",
                "subagent_name": "电力规范内容整理",
                "internal": True,
            }
        },
    )

    state = serialize_subagent_run_state(run)

    assert state["internal"] is True


def test_legacy_organizer_run_is_internal_even_without_runtime_flag():
    run = SimpleNamespace(
        id="child-run",
        agent_slug="power-spec-reviewer",
        conversation_thread_id="child-thread",
        status="completed",
        created_at=None,
        finished_at=None,
        error_message=None,
        input_payload={"runtime": {"tool_call_id": "tool-1", "subagent_name": "电力规范内容审查"}},
    )

    state = serialize_subagent_run_state(run)

    assert state["internal"] is True


def test_rejects_unknown_step_or_wrong_index():
    context = _context()

    assert (
        chat_service._accept_thinking_payload(
            {
                "type": "yuxi.thinking_step",
                "thinking": {"step_id": "unknown", "step_index": 1, "state": "completed"},
            },
            context,
        )
        is None
    )
    assert (
        chat_service._accept_thinking_payload(
            {
                "type": "yuxi.thinking_step",
                "thinking": {"step_id": "retrieve_kb", "step_index": 1, "state": "completed"},
            },
            context,
        )
        is None
    )
