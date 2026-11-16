# 处置方案责任班组改为按人员 JSON 责任区域匹配

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/maintenance_plan/sources.py

> 其他事实 Owner：图谱证据收集在 `graph_plan.py`，工具编排在 `tool.py`，装配与校验门在 `answer.py`，展示契约与固定文案在 `contracts.py`。

## 问题

数据侧把 `maintenance-plan/personnel.json` 改为按 `责任区域` 分组的班组：当前 5 个班组 104 人，结构为 `{"班组":[{"班组名称":...,"责任区域":[...],"人员":[...]}]}`。原实现从图谱 `运维组织 -[管辖]-> 设备实例` 推导责任班组，再按岗位角色从班组人员池随机抽样名单；新 JSON 没有岗位角色，且现场要求方案只显示责任班组、所需人数与工作负责人。继续抽样会把随机人员写成岗位名单，图谱管辖班组也可能与 JSON 责任区域指向不同班组。同时现场要求方案不再输出“五、数据说明”。

## 决策

1. **解析**：人员 JSON 解析为 `TeamRecord(name, areas, members)`，只接受 `{"班组":[...]}` 结构；其他结构或读取失败返回 `None`，不保留旧 `{"班组名称","人员"}` 结构的兼容层。
2. **责任班组**：设备 `层级路径` 拆成节点，从最近节点向根节点逐个与各班组的 `责任区域` 做归一化精确匹配；命中即取该班组。多个节点命中取最近节点；同一节点对应多个班组时取 JSON 顺序第一个。
3. **兜底**：未匹配时责任班组统一为 `电力巡护班组`，并从 JSON 取该班组的人员名单。
4. **工作负责人**：取命中班组 `人员` 数组第一个姓名，不随机、不按岗位筛选、不跨班组。
5. **所需人数**：仍为分支过滤后 `人员需求.人数` 合计；班组人数小于所需人数不阻断、不报错。
6. **输出**：方案第四段只渲染责任班组、所需人数、工作负责人三行；删除“五、数据说明”段，注记保留在结构化 `report.data_notes`，不再进入 Markdown。
7. **删除旧能力**：图谱 `运维组织/管辖` 责任班组查询、按岗位随机抽样、`PERSONNEL_SHORTAGE_MESSAGE` 整单失败、`TeamInfo`、`PersonnelAssignment.roles/shortage`。
8. **降级**：JSON 不可用 → 责任班组 `电力巡护班组`、工作负责人 `未接入`、注记 `PERSONNEL_DATA_MISSING_NOTE`；JSON 可用但兜底班组缺失或人员名单为空 → 工作负责人 `未接入`、注记 `PERSONNEL_TEAM_MISSING_NOTE`。

## 替代方案

- **保留图谱管辖班组**：与数据侧新口径冲突，且现场要求从 JSON 取班组，拒绝。
- **保留按岗位随机抽样**：新 JSON 没有岗位角色，抽样只能把随机人员写成岗位名单，拒绝。
- **班组人数不足继续整单失败**：方案只需要责任班组与负责人，所需人数来自图谱人数合计，抽样已删除，失败条件不存在，拒绝。
- **按检修作业场景名匹配班组**：新 JSON 只有 `责任区域`，没有场景字段，无法匹配，拒绝。
- **工作负责人取图谱班组负责人**：现场明确取 JSON 人员数组第一个人，拒绝。
- **保留“五、数据说明”**：用户明确要求不输出；注记保留在结构化报告，拒绝渲染。

## 后果

- 方案人员段不再分配具体检修人员，只给出责任班组与工作负责人；现场按班组自行分派。
- 所需人数与 JSON 班组人数脱钩：班组人数小于所需人数仍生成方案。
- 人员 JSON 结构成为运行时契约：回退旧结构会降级为“未接入”，不会报错、不会伪造负责人。
- 图谱证据收集少一次 `运维组织/管辖` 查询。
- 已落库的 `maintenance-plan-agent` 预设提示词不会自动刷新（`ensure_preset` 只写首次），部署后需按 [kg4 记录](./2026-11-05-maintenance-plan-kg4-scenario.md) 的方式覆写该行的 `description` 与 `config_json.context.system_prompt`。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 新 JSON 结构可解析为 5 个班组 104 人 | 旧结构/缺 `责任区域` 时被误解析 | `sources.py` | 真实 MinIO 回读（`BYTES 3162`，5 班组、104 人、每班组带责任区域） | `班组` 不是数组时 `_read_teams` 返回 `None` | Passed |
| 责任区域按路径就近匹配 | 取首个匹配或按场景名匹配导致错班组 | `sources.py` | `test_assign_personnel_matches_nearest_path_node`、`test_assign_personnel_prefers_deeper_path_node`；真实数据：`35kV#3变电站`→3号站监控调度班组/刘昕宇，`110kV变电站`→110站监控调度班组/马斌辉 | 无匹配路径必须回落电力巡护班组 | Passed |
| 未匹配统一电力巡护班组 | 回退图谱班组或空值 | `sources.py` | `test_assign_personnel_falls_back_to_patrol_team`；真实数据：`岭开线`→电力巡护班组/曹世杰、未知站→电力巡护班组/曹世杰 | JSON 无该班组时负责人 `未接入` | Passed |
| 方案第四段只输出三行且无“五、数据说明” | 岗位名单或数据说明仍被渲染 | `answer.py` | `test_assemble_report_and_markdown_include_branch_and_sections`、`test_full_flow_returns_verified_plan`、`test_assemble_report_notes_user_confirmed_branch` | 篡改责任班组/负责人被 `verify_report` 拒绝 | Passed |
| 所需人数不变且不再按人数否决 | `named == total` 误杀或抽出随机名单 | `answer.py` | `test_verify_report_accepts_single_leader_for_total_headcount`、`test_small_team_still_generates_plan` | 班组 1 人、所需 16 人仍 `answer_verified=True` | Passed |
| 图表不再查询班组管辖 | 仍发 `运维组织/管辖` Cypher | `graph_plan.py` | `test_collect_scene_evidence_does_not_query_team_nodes` | `grep -rn "运维组织\|管辖" package/yuxi/knowledge/maintenance_plan` 无运行时命中 | Passed |
| 缺失数据不阻断方案 | 抛异常或伪造负责人 | `sources.py` | `test_assign_personnel_without_minio_data_returns_fallback_team`、`test_assign_personnel_without_usable_members_marks_note` | `load_teams` 返回 `None` 时仍渲染三行 | Passed |
| 维护方案单测全绿 | 旧字段/文案残留导致导入或断言失败 | `test/unit/knowledge` | `docker compose exec -T api uv run --no-sync --group test pytest test/unit/knowledge -k maintenance_plan -q`：204 passed；服务器 `yuxi-api-1` 内同命令：204 passed | 全量单测回归 | Passed |
| 最新后端状态已部署到服务器 | 服务器仍运行旧代码或容器未重启 | `/opt/Yuxi/backend`、`yuxi-api-1`、`yuxi-worker-1` | 63 个后端文件同步后远端/本地 SHA256 清单一致（734 个文件，0 差异）；备份 `/opt/Yuxi/deploy-backups/backend-pre-team-20261113-175531.tar.gz`；重启后 `/api/system/ready` 返回 `ready`，所有必需组件 `ok`；容器内 204 个维护方案单测通过 | 远端存在本地没有的后端文件时不得覆盖 | Passed |
| 服务器真实 MinIO 可产出三行人员安排 | 部署后仍用旧结构或匹配错班组 | `sources.py` | 容器内调用 `load_teams(force=True)` + `assign_personnel`：5 班组 104 人；`35kV#3变电站`→3号站监控调度班组/刘昕宇，`35kV#1变电站`→1号站监控调度班组/王伟，`110kV变电站`→110站监控调度班组/马斌辉，`岭开线`/未知站→电力巡护班组/曹世杰；`format_personnel` 输出 `工作负责人：<姓名>` | JSON 缺字段时负责人 `未接入` | Passed |

容器内执行：

```bash
docker compose exec -T api uv run --no-sync --group test pytest test/unit/knowledge -k maintenance_plan -q
docker compose exec -T api uv run --no-sync --group test pytest test/unit -m "not slow"
docker compose exec -T api sh -lc 'cd /app && uv run --no-sync --with ruff ruff check --no-cache package/yuxi/knowledge/maintenance_plan package/yuxi/agents/presets/maintenance_plan.py'
docker compose exec -T api sh -lc 'cd /app && uv run --no-sync --with ruff ruff format --no-cache package/yuxi/knowledge/maintenance_plan package/yuxi/agents/presets/maintenance_plan.py --check'
```

本机全量单测 `test/unit -m "not slow"` 为 3125 passed、54 skipped、4 failed；4 个失败全部在 `test_power_spec_formatting.py`，来自工作区中已有的 `power_spec/answer.py` 关闭 PDF 标记改动（代码注释与 TODO 可见），与本记录改动面无关。工程契约脚本 `python3 scripts/verify_engineering_contracts.py` 通过；`scripts.test_verify_engineering_contracts` 在 Windows 主机因符号链接权限（WinError 1314）1 个用例报错、其余 61 个通过，需在 Linux/容器内复核。文档构建 `pnpm run build` 通过。改动文件的 Ruff check 与 format check 全绿；全仓 `ruff check package` 仍有本次变更前已存在的 2 处 E501 与 1 处格式问题（`config/options.py`、`power_spec/answer.py`、`fault_trace/workflow.py`），不属于本记录改动面。

未验证范围：未执行真实 HTTP/LangGraph 会话与发布链路 E2E；服务器上通过容器内单元测试、`/api/system/ready` 与真实 MinIO 探针验证，未用真实登录会话触发方案卡片。已落库的 `maintenance-plan-agent` 预设行仍保留旧系统提示词（其中引用已删除的“人员数据不足”固定文案），不影响本功能，但需要时按 kg4 记录覆写。
