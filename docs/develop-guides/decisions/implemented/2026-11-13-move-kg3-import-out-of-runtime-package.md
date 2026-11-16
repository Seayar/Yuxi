# 处置方案 kg3 导入资产移出运行时包

状态：implemented
类型：simplification
Owner：backend/scripts/legacy_kg3_seed/seed.py

## 问题

`backend/package/yuxi/knowledge/maintenance_plan/` 同时存放运行时链路（`contracts`、`graph_plan`、`branch`、`sources`、`intent`、`answer`、`tool`）与 kg3 历史导入资产（`seed.py`、`preflight.py`、`data/` 下的六份 JSON）。导入资产没有任何运行时消费者，但目录位置、模块名和包描述让人以为它们参与方案生成：读 `knowledge/maintenance_plan/` 得不到“运行时如何产出方案”的完整判断，也无法从目录区分哪些代码随请求执行。

## 决策

1. **位置**：`seed.py`、`preflight.py` 与 `data/` 移到 `backend/scripts/legacy_kg3_seed/`，与 `backend/scripts` 下其它运维脚本同层，不再属于 `yuxi` 运行时包。开发 compose 已把 `backend/scripts` 挂载到 `/app/scripts`，容器内仍可运行。
2. **导入方向**：`preflight.py` 继续从 `yuxi.knowledge.maintenance_plan.contracts` 读 `DEVICE_TYPES`，设备类型白名单仍只有一个 Owner；运行时模块不反向导入本包，`scripts` 只依赖运行时契约。
3. **调用方式**：离线预检与写图改为 `python -m scripts.legacy_kg3_seed.preflight` 与 `python -m scripts.legacy_kg3_seed.seed`，在仓库 `backend/` 目录（容器内 `/app`）执行；不保留旧模块路径的兼容别名。
4. **测试**：两个单测与假 driver 一起移到 `backend/test/unit/scripts/`，与 `test_evaluate_power_spec_answers.py` 同级；测试文件名保留 `maintenance_plan` 前缀，`-k maintenance_plan` 在 `test/unit/knowledge` 之外仍可选中。

## 替代方案

- **直接删除**：kg3 仍是可回指的配置图，`data/` 是历史导入的唯一来源，现场是否还要补写 kg3 数据未确认；删除会同时丢掉数据与迁移能力。
- **原样留在运行时包**：本次要解决的问题就是位置与命名带来的误判，保留则仍需靠文档解释“这不是运行时”。
- **移到仓库根 `scripts/`**：该目录不在 api/worker 容器的挂载范围内，容器内无法运行；根 `scripts/` 是仓库级契约检查工具，混入依赖 `neo4j` 和运行态配置的数据导入器不符合其职责。
- **复制到 `scripts/` 后保留原文件**：两份可独立编辑的数据导入实现会漂移，违反单一 Owner，未采用。

## 后果

- `yuxi.knowledge.maintenance_plan` 只保留运行时模块，包描述指向新位置；“读 knowledge 等于读运行时”在处置方案目录内成立。
- 生产镜像（`docker/api.Dockerfile` 只复制 `package/` 与 `server/`）不包含 `backend/scripts`，该 CLI 不再随生产镜像分发；历史导入在开发 compose 或本地工作区执行。kg3 不参与运行时，生产环境不需要该命令。
- `ruff check package` 与 `ruff format package --check` 不再覆盖这两个模块，与 `backend/scripts` 其它脚本一致；行为和回归由 `test/unit/scripts` 的单测覆盖。
- 运维命令变化：`python -m yuxi.knowledge.maintenance_plan.seed` 不再存在。需要旧命令的现场必须改用新路径，代码不提供转发模块。

## 验证

| 主张 | 证据 | 结果 |
|---|---|---|
| 导出的模块在新路径可导入并通过单测 | 容器内 `uv run --no-sync --group test pytest test/unit/scripts/test_maintenance_plan_seed.py test/unit/scripts/test_maintenance_plan_preflight.py -q` | Passed（37 passed） |
| 处置方案目录内的既有测试不受影响 | `pytest test/unit/scripts -q` | Passed（38 passed） |
| 运行时测试选择集不受移动影响 | `pytest test/unit/knowledge -k maintenance_plan -q` | Passed（198 passed，689 deselected） |
| 导入 CLI 在新路径可运行 | 容器内 `python -m scripts.legacy_kg3_seed.seed --uri bolt://fake:7688 --kg-ref test/.kg_ref_probe.json --dry-run` | Passed（`status=dry_run`，59 模板 / 14 安全距离 / 10 班组 / 37 备件；缺 `--kg-ref` 时退出码 1） |
| 移动后的代码可读可运行 | `python -m compileall` 于 `backend/scripts/legacy_kg3_seed`；容器内 `ruff check --no-cache scripts/legacy_kg3_seed` 与 `ruff format --no-cache --check` | Passed |
| 文档与决策格式有效 | `python3 scripts/verify_engineering_contracts.py`；`cd docs && pnpm run build` | Passed（143 decisions / 196 docs） |
| 文档与代码不残留旧路径 | 检索 `maintenance_plan.seed`、`maintenance_plan/seed` | Passed（仅 `docs/vibe/` 本地忽略文件命中） |

`python -m unittest scripts.test_verify_engineering_contracts` 在 Windows 宿主机运行 62 项，其中 1 项因当前账户无创建符号链接权限报错（`WinError 1314`，环境限制，与本次移动无关）；该项未在容器内执行。`test/unit/scripts/test_maintenance_plan_seed.py` 在移动前的版本就不满足 `ruff format`，本次未改动其既有排版，仅更新导入与文件头。

旧能力不存在：`backend/package/yuxi/knowledge/maintenance_plan/` 下不再有 `seed.py`、`preflight.py`、`data/`；`python -m yuxi.knowledge.maintenance_plan.seed` 与 `maintenance_plan.preflight` 导入路径不存在。

重新引入条件：出现真实的流水线或运行时消费者，需要从仓库内 JSON 定期灌图时，把该包移回可部署路径（`package/` 或镜像会复制的目录）并在 CI 增加对应入口的验证；仅当该消费者存在时才重新引入。
