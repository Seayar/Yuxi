"""子智能体采样温度的声明与传递。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.agents.buildin.subagent import graph as subagent_graph
from yuxi.agents.buildin.subagent.context import SubAgentContext
from yuxi.agents.context import filter_declared_config
from yuxi.services.agent_run_service import load_agent_run_context


def _loaded_context(config_context: dict):
    """模拟按落库配置实例化子智能体运行上下文。"""

    agent_item = SimpleNamespace(config_json={"context": dict(config_context)})
    agent_backend = SimpleNamespace(context_schema=SubAgentContext)
    return load_agent_run_context(agent_item, agent_backend)


@pytest.mark.unit
def test_subagent_temperature_from_persisted_config_reaches_model_kwargs():
    """落库配置里的 temperature 会进入运行上下文并下发给模型。"""

    context = _loaded_context({"temperature": 0})

    assert context.temperature == 0
    assert subagent_graph._subagent_model_kwargs(context) == {"temperature": 0}


@pytest.mark.unit
def test_subagent_without_temperature_keeps_service_default():
    """未声明温度时不向模型下发该参数，保持服务默认采样行为。"""

    context = _loaded_context({})

    assert context.temperature is None
    assert subagent_graph._subagent_model_kwargs(context) == {}


@pytest.mark.unit
def test_undeclared_model_parameter_is_dropped():
    """负向：未声明的模型参数键不会进入上下文，也不会传给模型。"""

    assert filter_declared_config({"context": {"temperature": 0, "sampling_seed": 7}}, SubAgentContext) == {
        "context": {"temperature": 0}
    }

    context = _loaded_context({"sampling_seed": 7})

    assert not hasattr(context, "sampling_seed")
    assert subagent_graph._subagent_model_kwargs(context) == {}
