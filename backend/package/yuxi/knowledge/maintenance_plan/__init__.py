"""处置方案生成模块。

各能力按子模块提供：``contracts``（契约与固定文案）、``graph_plan``（图谱证据）、
``intent``（意图识别与输入约束）、``answer``（方案装配）、``preflight``/``seed``
（图谱导入与预检）、``tool``（工具入口）。
包根不再重导出具体符号：``graph_plan`` 依赖 ``yuxi.agents.toolkits``，在包 ``__init__``
里导入会与 ``toolkits`` 注册入口形成循环导入。
"""
