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


@pytest.mark.unit
def test_power_spec_reviewer_preset_is_faithfulness_only():
    """负向：审查子智能体只核对忠实性，不判断条款是否切题或覆盖是否充分。"""

    found = {preset.slug: preset for preset in discover_agent_presets()}
    prompt = found[POWER_SPEC_REVIEWER_AGENT_SLUG].context["system_prompt"]

    assert "忠实" in prompt
    assert "不判断条款是否切题" in prompt
    assert "无关内容" not in prompt
    assert "遗漏" not in prompt
    assert "不要评价条款取舍、覆盖范围或答案完整性" in prompt


@pytest.mark.unit
def test_reviewer_preset_and_orchestration_instruction_share_faithfulness_contract():
    """漂移守卫：编排层 REVIEWER_INSTRUCTION 与落库预设必须携带同一组忠实性标记，
    任何一方单独改写（尤其改回相关性检查）都会让本用例变红。"""

    from yuxi.knowledge.power_spec.answer import REVIEWER_INSTRUCTION

    found = {preset.slug: preset for preset in discover_agent_presets()}
    prompt = found[POWER_SPEC_REVIEWER_AGENT_SLUG].context["system_prompt"]

    for marker in ("忠实", "编造", "语义改变", "不判断条款是否切题", "不要评价条款取舍、覆盖范围或答案完整性"):
        assert marker in REVIEWER_INSTRUCTION, marker
        assert marker in prompt, marker
    for banned in ("无关内容", "遗漏"):
        assert banned not in REVIEWER_INSTRUCTION, banned
        assert banned not in prompt, banned
