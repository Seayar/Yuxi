"""知识模块必须能在全新解释器里单独导入。

这些模块会被 ``yuxi.agents.toolkits`` 在包初始化阶段导入；若它们在模块级反向导入
``yuxi.agents`` 或另一个正在初始化的兄弟模块，就会出现只在特定导入顺序下才暴露的
循环导入。子进程导入是唯一能证明“顺序无关”的 oracle。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

MODULES = (
    "yuxi.knowledge.fault_trace.workflow",
    "yuxi.knowledge.fault_trace.tool",
    "yuxi.knowledge.maintenance_plan.graph_plan",
    "yuxi.knowledge.maintenance_plan.tool",
    "yuxi.knowledge.power_spec.tool",
)


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_in_fresh_interpreter(module: str) -> None:
    """每个模块都必须能在没有被其他模块预热过的解释器里导入成功。"""

    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
