"""处置方案 kg3 历史导入资产：seed 数据、离线预检与写图 CLI。

``seed`` / ``preflight`` / ``data`` 只服务历史 ``kg3`` 图的一次性迁移，不在运行时处置方案链路
里；运行时读图与方案装配在 ``yuxi.knowledge.maintenance_plan``。在仓库 ``backend/`` 目录
（容器内为 ``/app``）用 ``python -m scripts.legacy_kg3_seed.seed`` 运行。
"""
