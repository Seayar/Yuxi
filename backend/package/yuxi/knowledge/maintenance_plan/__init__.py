"""处置方案生成模块（检修作业场景）。

各能力按子模块提供：``contracts``（契约与固定文案）、``graph_plan``（设备/场景/证据检索）、
``branch``（Redis 分支判定）、``sources``（MinIO 工器具/人员数据）、``intent``（意图识别与
输入约束）、``answer``（方案装配与校验）、``tool``（工具入口）；kg3 历史导入资产
（``seed``/``preflight``/``data``）已移出本包，位于 ``backend/scripts/legacy_kg3_seed/``，
不属于运行时链路。
包根不再重导出具体符号：``graph_plan`` 依赖 ``yuxi.agents.toolkits``，在包 ``__init__``
里导入会与 ``toolkits`` 注册入口形成循环导入。
"""
