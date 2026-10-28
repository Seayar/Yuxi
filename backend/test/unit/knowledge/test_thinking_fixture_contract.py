"""四步思考事件样例必须与注册表一致，防止契约样例随实现漂移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuxi.agents.thinking import THINKING_STATES, get_thinking_steps
from yuxi.knowledge.fault_trace import contracts as _fault_trace_contracts  # noqa: F401 - 导入即注册
from yuxi.knowledge.power_spec import contracts as _power_spec_contracts  # noqa: F401 - 导入即注册

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "thinking"

CASES = (
    ("fault-trace-thinking-v2.json", "fault-trace-agent"),
    ("power-spec-thinking-v2.json", "power-spec-agent"),
)


@pytest.mark.parametrize(("filename", "agent_slug"), CASES)
def test_fixture_events_match_registered_steps(filename: str, agent_slug: str) -> None:
    """样例里的步骤、标题、顺序与状态必须与当前注册表和状态词表一致。"""

    payload = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
    steps = get_thinking_steps(agent_slug)
    indexed = {step.step_id: (index, step) for index, step in enumerate(steps, start=1)}
    events = payload["events"]

    assert payload["agent"] == agent_slug
    assert events
    for event in events:
        assert event["agent"] == agent_slug
        assert event["state"] in THINKING_STATES
        assert event["step_id"] in indexed, event["step_id"]
        expected_index, spec = indexed[event["step_id"]]
        assert event["step_index"] == expected_index
        assert event["title"] == spec.title
    assert {event["step_id"] for event in events} == set(indexed)
