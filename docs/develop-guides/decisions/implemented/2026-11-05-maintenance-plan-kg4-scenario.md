# 处置方案切换到 kg4 检修作业场景

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/maintenance_plan/tool.py

## 问题

处置方案生成原先读 TuGraph `kg3` 的 `作业模板`，按任务类型（更换/检修/试验/巡视）装配六段式方案。现场已把处置方案知识迁到 `kg4`：设备实例经 `属于` 关联设备类型，设备类型经 `包含作业` 关联检修作业场景；场景自身带 `分支判定维度/分支1条件/分支2条件`，步骤与分支无关，安全措施、资源需求、人员需求按 `是否通用` 与 `适用分支值` 取当前分支内容。旧链路在 kg4 上取不到设备类型（kg4 无 `Scheme1标签`），也查不到 `作业模板`，只能返回“暂无作业规程”。

## 决策

1. **配置**：`fault_trace_opts` 增加 `maintenance_plan_graph` 字段（默认 `kg4`，环境变量 `MAINTENANCE_PLAN_GRAPH`）。故障溯源继续用 `graph`（kg3），处置方案用新字段；TuGraph 地址、账号、密码共用。
2. **设备与场景**：`search_devices` 按名称/层级路径召回，再用一次 `属于` 查询为候选补设备类型，只保留有类型边的设备；`find_scenes` 读 `设备类型 -[包含作业]-> 检修作业场景`。场景按用户原文与名称/说明的字符二元组重叠打分；最高分并列或全为 0 时发 `interrupt` 选择卡片。
3. **分支**：`branch.py` 读 Redis `{台账ID}:{分支判定维度}`（台账 ID 来自 `设备实例.台账ID列表`，多个 ID 全部尝试），值必须与 `分支1条件`/`分支2条件` 归一后精确匹配。Redis 未配置、无 key、不可用或值非法时降级为让用户确认分支，不默认分支；用户确认的分支在方案“数据说明”中标注。
4. **装配**：步骤按 `步骤序号` 排序，输出 `序号+操作内容`；安全措施输出 `名称+具体内容`；资源按 `数量>0` 输出名称+数量+单位、`数量=0` 输出名称+数量要求，并附库存标记；人员按 `岗位角色` 分组求和，从人员池随机抽样。四段内容加设备/场景/分支头，不再保留 kg3 的停电范围、安全距离、验收标准。
5. **外部数据**：工器具与人员 JSON 从 MinIO 的 `maintenance-plan` bucket 读取（与 `fault-assets` 平级，默认 `tools.json` / `personnel.json`，可用 `MAINTENANCE_PLAN_*_BUCKET/OBJECT` 覆盖）。读取失败不阻断方案：资源标注“库存数据未接入”，人员只输出岗位与人数；人员池不足时返回固定“人员数据不足”文案。
6. **校验与发布**：`verify_report` 校验分支值、四段条目与证据一致、人员名单数量与合计一致、报告数字能在证据中找到；工具侧与 `chat_service` 发布门仍只发布 `answer_verified is True` 的载荷。
7. **kg3 资产**：`seed.py`/`preflight.py`/`data/` 保留为 kg3 历史导入资产，不再是处置方案运行时路径；其标签/关系常量移到 `seed.py` 自己拥有，`contracts` 只拥有 kg4 运行时 schema。
8. **思考过程**：`contracts` 为 `maintenance-plan-agent` 注册四步（解析任务需求、检索知识图谱、组装方案模板、生成标准化方案）；工具用 `ThinkingEmitter` 在对应阶段发射 `started/completed/failed/waiting` 事件，并在结果载荷附带 `thinking_steps` 快照。设备/场景/分支等待用户选择时标记 `waiting`，校验失败或数据不足标记 `failed`。

## 替代方案

- **保留 kg3/kg4 双路径**：需要维护两套 schema、意图与装配，当前只有 kg4 一个生产消费方，未采用。
- **分支缺失时默认分支1**：会把错误的检修措施、资源与人员发布给现场，未采用；改为用户确认。
- **工器具/人员 JSON 缺失时阻断整份方案**：用户要求先打通图谱链路并预留外部接口，未采用；改为在方案中显式标注数据缺口。
- **人员不足时跨班组凑人**：会把非责任班组人员排进方案，未采用；返回固定失败文案。
- **把 `maintenance_plan` 单独建一套 TuGraph 连接配置**：会重复维护同一服务地址与凭据，未采用。

## 后果

- kg4 的 30 个检修作业场景中，23 个有对应设备实例；隔离开关、电压互感器、电抗器、电容器、熔断器、接地装置的 7 个场景当前无设备实例，设备检索无法命中，需要补设备数据。
- `人员需求` 节点没有“责任班组”属性；责任班组从 `运维组织 -[管辖]-> 设备实例`（只取 `组织类型=班组`）推导，人员池按同名班组过滤。
- 分支 Redis 已录入 30 个场景的 `{台账ID}:{分支判定维度}` key，但 key 绑定的是各场景的样例设备：用户必须问到该设备（或同一设备的多台账 ID）才能命中；`3k18` 等未录入的设备仍会走用户确认分支。kg4 中 `台账ID列表=补充数据` 的设备会按名称+层级回查 `fault-assets/ledger/devices.json` 补全台账 ID；缺 `属于` 边的线路/间隔/PT 设备按台账分类做有限类型回退（中压线路→架空线路、母联/分段开关间隔→断路器、电压互感器间隔→电压互感器）。
- 仍无法命中的是图谱本身缺设备或类型的数据缺口：隔离开关、电压互感器、电容器、熔断器、接地装置等类型的部分场景；`柴发PT柜`、`岭开线进线间隔_1101` 等台账分类与场景设备类型不一致的设备只能走用户确认分支。
- 引导选择等待期间，Redis 分支值、台账数据或场景内容若发生变化，以工具重放时刻读取的状态为准；卡片发出前的首次查询结果不作为答复后的执行依据。
- 工器具/人员 JSON 已由数据团队放到 MinIO 的 `maintenance-plan` bucket（与故障溯源的 `fault-assets` 平级）：`tools.json`（304 条，`quantity>0` 判在库）与 `personnel.json`（8 个班组 106 人，按班组分组）。代码上也由平级的 `maintenance_plan/sources.py` 拥有，不放进 `fault_trace`。读取失败仍会降级并在方案中标注；更新对象后进程内缓存最多 5 分钟生效。
- 人员 JSON 的班组人数与图推导班组不完全匹配：如 A1 的“1号站监控调度班组”在 JSON 中只有 1 人，而场景需要 16 人；当前策略是责任班组不足时从全体人员池抽取并在数据说明中标注，不跨班组伪造责任人。
- 方案不再包含停电范围、安全距离、验收标准。kg4 没有这些节点，保留空段会误导现场。
- `maintenance-plan-agent` 预设的提示词与描述随 kg4 改写；`ensure_preset` 只在首次落库时写入，已存在的该行必须按 `backend/package/yuxi/agents/presets/maintenance_plan.py` 当前值覆写 `description` 与 `config_json.context.system_prompt`，否则模型仍按 kg3 六段式与已删除的旧固定文案行动。

## 验证

| 主张 | 证据 | 负向案例 | 状态 |
|---|---|---|---|
| kg4 标签/关系/数据可支撑 8 步流程 | 只读 Cypher：30 场景、193 步骤、120 安全措施、304 资源、124 人员；每场景分支字段非空 | 场景缺子节点/分支条件为空时返回固定“暂无场景”而非空方案 | 已核验 |
| 设备类型来自 `属于`，不依赖 `Scheme1标签` | kg4 `Scheme1标签` 节点数 0、`属于` 边覆盖 990 个设备；单测覆盖无类型节点被过滤 | 只按名称召回未过滤区域节点 | 单测 |
| 分支优先 Redis、缺失降级用户确认 | `test_maintenance_plan_branch.py` 覆盖 ok/missing/unavailable/invalid/冲突；工具单测覆盖用户选择 | 默认分支1 | 单测 |
| 资源/人员按通用与分支过滤 | 图检索单测断言只保留通用+当前分支；`数量=0` 与空单位有格式化用例 | 混入另一分支条目 | 单测 |
| MinIO 工器具/人员结构可解析 | 真实 MinIO 回读：304 条工器具（296 在库、8 不在库）、106 人/8 班组；`汽车起重机` 命中在库、`25 号变压器油` 命中不在库；3 人抽样成功 | 解析失败时静默丢弃资源或人员段 | Passed（真实 MinIO 只读） |
| Redis 分支值可被真实设备命中 | 真实 Redis 55 个 key（30 个分支 key） + kg4：`主变1` A1 命中 `已高于环境温度`（分支2/14 人）、`主变2` A3 命中 `母线带电`（分支2/7 人）、`10kVⅠ段母线` A4 命中 `Ⅰ、Ⅱ 段同室并列`（7 人）、`隔离_300` A5/B1 命中分支1、`馈线八_922` B4/C1 分别命中各自 `柜型结构` 值、`3B-6K` D3/D7 命中、`馈线一_912` A2 命中 | 多台账 ID 中一个值不匹配就把整次判为非法；`补充数据` 占位导致 key 前缀错误 | Passed（真实 Redis + Bolt 只读） |
| 四步思考过程按注册顺序发射 | 单测断言结果 `thinking_steps` 为“解析任务需求/检索知识图谱/组装方案模板/生成标准化方案”且全部 `completed`；真实链路结果同样返回四步完成态，等待选择时发 `waiting`、校验失败发 `failed` | 步骤标题/顺序与注册表不一致，或方案成功但思考步骤缺失 | 单测 + 真实链路 |
| 未校验方案不外泄 | `verify_report` 分支/条目/人数/数字校验；发布门测试保留 | 篡改分支或伪造资源仍发布 | 单测 |
| 真实 kg4 查询可执行 | 用项目查询函数经 Bolt 只读执行：`3k18` 命中 2 个开关柜候选、5 个场景，场景 A2 得分 15 排第一；证据为 7 步骤/3 安全措施/10 资源/7 人；`verify_report` 为 True | — | Passed（只读 Bolt 实测） |
| 已落库的 maintenance-plan-agent 行刷新后使用新提示词 | 旧提示词继续引用 kg3 六段式与已删除的固定文案；`ensure_preset` 只写首次落库。运维后真实 PostgreSQL 回读 `config_json.context.system_prompt` 含「检修作业场景」 | 刷新前回读到旧提示词 | Not run |

容器内执行：

```bash
docker compose exec api uv run --group test pytest test/unit/knowledge -k maintenance_plan
docker compose exec api uv run ruff check package
```

本轮已在容器实测：`uv run --no-sync --group test pytest test/unit/knowledge -k maintenance_plan -q` 233 passed——覆盖 `verify_report` 每类检查（分支、步骤、安全措施、资源、人员名单、合计、数值）各一个绑定 warning 文案的定向负向用例、思考步骤 waiting/failed 状态、结果缓存按 query_text 区分、同名场景并列最高分发中断、场景子节点全空返回固定“暂无场景”、同岗位多需求节点先合计再抽样、`NO_PERMISSION_MESSAGE` 透传；kg3 seed/preflight 与发布门用例同步通过。后端全量 unit `test/unit -m "not slow"` 3115 passed、54 skipped。ruff 经 `uv run --no-sync --with ruff` 在容器实测 `check package` 与 `format package --check` 全绿；真实 HTTP/LangGraph 中断 E2E 与维护方案预设落库刷新的 PostgreSQL 回读未验证。桩测试不能替代真实 `@tool`、LangGraph 中断与 HTTP 链路。未录入分支 key 的设备仍走用户确认分支。

## 相关决策

- [处置方案智能体](./2026-10-16-maintenance-plan-agent.md)：本记录取代该记录的 kg3 作业模板六段式运行时；kg3 的 seed/preflight/data 保留为历史导入资产。
- [故障图谱迁移到 kg3 并移除公开故障路由](./2026-10-28-fault-trace-kg3-and-route-removal.md)：故障溯源仍使用 kg3；自本记录起处置方案使用 kg4，两者共用 TuGraph 连接配置、图名分离。
