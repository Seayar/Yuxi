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


@pytest.mark.unit
def test_power_spec_organizer_preset_fixes_sampling_temperature():
    """整理输出需要稳定复现，预设固定采样温度；审查子智能体保持服务默认。"""

    found = {preset.slug: preset for preset in discover_agent_presets()}

    assert found[POWER_SPEC_ORGANIZER_AGENT_SLUG].context["temperature"] == 0
    assert "temperature" not in found[POWER_SPEC_REVIEWER_AGENT_SLUG].context


@pytest.mark.unit
def test_power_spec_organizer_preset_uses_dimensions_from_caller():
    """负向：整理子智能体的系统提示词不再要求它自行判断问题维度。"""

    found = {preset.slug: preset for preset in discover_agent_presets()}
    prompt = found[POWER_SPEC_ORGANIZER_AGENT_SLUG].context["system_prompt"]

    assert "先判断用户问的是哪个维度" not in prompt
    assert "关注维度由调用方给出" in prompt
    assert "占位说明" in prompt
