"""电力规范内置角色的预置定义。"""

import pytest

from yuxi.agents.presets import discover_agent_presets

POWER_SPEC_AGENT_SLUG = "power-spec-agent"
POWER_SPEC_ORGANIZER_AGENT_SLUG = "power-spec-organizer"
POWER_SPEC_REVIEWER_AGENT_SLUG = "power-spec-reviewer"


@pytest.mark.unit
def test_power_spec_agent_preset_declares_main_agent_contract():
    """主 Agent 预置声明知识库与技能，且不作为子智能体暴露。"""

    found = {preset.slug: preset for preset in discover_agent_presets()}
    preset = found[POWER_SPEC_AGENT_SLUG]

    assert preset.backend_id == "ChatbotAgent"
    assert preset.context["skills"] == ["knowledge-base", "power-spec"]
    assert preset.context["preload_skills"] == ["power-spec"]
    assert preset.context["max_execution_steps"] == 300
    assert preset.context["system_prompt"].strip()
    assert "knowledges" not in preset.context


@pytest.mark.unit
def test_power_spec_answer_subagent_presets_are_subagents():
    """整理与审查角色使用子智能体后端，并各自携带提示词。"""

    found = {preset.slug: preset for preset in discover_agent_presets()}

    for slug in (POWER_SPEC_ORGANIZER_AGENT_SLUG, POWER_SPEC_REVIEWER_AGENT_SLUG):
        preset = found[slug]
        assert preset.backend_id == "SubAgentBackend"
        assert preset.context["system_prompt"].strip()
