# 设备预测维护智能体（device-prediction-agent）

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/device_prediction/tool.py

## 问题

`srs/原型V0.1.0.0.html` 模块五「设备预测维护」与 `srs/需求宣讲V0.1.0.0.xlsx` 的「设备健康评估」页定义了综合评估报告：①设备健康评估（健康评分、健康等级、状态解读、监测指标列表）②故障预测（预测故障类型、发生概率、预计发生时间、退化趋势）③可靠性与风险评估（故障率、平均停运时间）④维护计划建议（维护时机、所需资源、注意事项）。数据流是“按名称检索设备 ID → 把 ID 分别传给 LSTM 故障预测服务与设备健康评估服务 → 按预测故障类型在知识图谱中匹配所需资源与注意事项”。信息不完整时按方案二（多候选引导选择）与方案三（反问澄清）继续对话。需求方后续把原「维护内容」字段从维护计划建议中移除，本智能体的报告契约不再包含该字段。

平台已有电力规范检索、故障原因溯源与处置方案生成三个知识型智能体，但没有预测维护能力：没有对应 Agent（技能/预设/工具/发布门），也没有保存这些字段的图谱。设备与维护知识保存在新的 TuGraph 知识图谱 `kg5`，本智能体的图谱读取只指向 `kg5`，不读 `kg3`、`kg4`。

## 决策

按处置方案生成智能体的同款装配路径新增「设备预测维护」智能体，六条链路：

1. **入口链路**：工具 `predict_device`（`yuxi/knowledge/device_prediction/tool.py`）接收用户问题原文，经 `intent.extract_prediction_intent` 解析设备名称、设备类型、电压等级与位置线索（模型结构化 JSON + 规则兜底），既无设备名称也无设备类型时返回固定澄清文案；设备候选不唯一时发 `interrupt` 选择卡片（载荷沿用既有 `source + questions[]` 协议）。思考过程四步与原型一致：加载设备档案、健康评估计算、趋势预测、生成维护建议。结果按规范化后的问题文本在运行上下文里缓存：同一问题在同一轮内不重复调用外部服务，用户换成新的描述重问时不会被上一次结果挡住。
2. **图谱链路**：**图谱只读 `kg5`**，不读 `kg3`、`kg4`。`graph.search_devices` 在 `设备实例` 上按名称（含“1号→#1/1#”“主变→主变压器”同义展开）、层级路径与位置线索召回并打分，`设备类型` 只作匹配加分项，缺 `属于` 边不影响召回。`graph.resolve_device_id` 先取设备节点的 `台账ID列表`（跳过“补充数据”等占位值）；只有节点上的 ID 不可用时，才按**精确名称 + 层级祖先**回查三区设备台账（MinIO `fault-assets/ledger/devices.json`，与故障溯源共用同一对象），台账只接受与设备名称完全一致的行、不采用子串匹配；两处都拿不到就返回固定文案；图内部节点 id（`LGR#…`）永不作为外发预测服务的设备 ID（设备选择卡片的选项值仍用节点 id 作回填标识，与处置方案同款既有协议）。图名走 `fault_trace_opts.device_prediction_graph`（缺省 `kg5`），地址与凭据复用同一 TuGraph 连接。
3. **维护知识链路**：匹配发生在 `设备类型 -[具有现象]-> 故障现象` 的候选集内——先按 `故障现象.故障码` 精确匹配，再按现象 `名称` 精确、名称互相包含、字符二元组重叠（阈值 0.5）依次降级；预测故障类型是“高温过热/局部放电”这类复合写法时按分隔符拆开取最高分；同分取名称更长（更具体）的现象，再按节点 id 稳定排序。命中后 `所需资源` 与 `注意事项` 直接取现象属性。设备没有 `属于` 边时退回全图现象匹配，但只接受故障码/名称精确匹配（不做跨设备类型的模糊匹配），并在数据说明中标注“设备类型缺失”。报告在维护段落展示匹配到的故障现象名。
4. **预测服务链路**：`services.fetch_health_assessment` 与 `services.fetch_fault_prediction` 用 `asyncio.gather` 并行调用 `GET /api/device-health/{deviceId}` 与 `GET /api/prediction/predict/{deviceId}`；设备 ID 里的 `#` 等字符按 URL 路径编码。服务地址走管理员系统配置 `device_prediction_opts`，未配置、不可达（含非 200）、数据不完整（响应不是 JSON 对象或缺必需字段）分别投影为“未配置/暂不可用/数据不完整”三类固定文案；两个服务对数据集中不存在的设备都返回 404（预测服务 `{"detail": …}`、健康评估服务 `{"code": "NOT_FOUND", "message": …}`），因此 404 单独投影为“服务中没有该设备的数据”，与“服务暂不可用”区分。故障率与平均停运时间是需求标注的低优先级字段，服务返回纯数值时按需求口径补“次/年”“小时/次”，缺失时按实际缺口写入数据说明；`maintenance_time` 缺失时按预计发生时间推导“在故障发生前完成维护”的时机（区间取早界），并在报告中标注为推导值。
5. **装配与校验链路**：`answer.produce_prediction_answer` 用证据确定性装配四段报告与 Markdown，不发明数值；条目自带编号时按原编号渲染，不叠加序号。“状态解读”是需求表里唯一由大模型生成的字段（`answer.generate_status_readout`）：模型只看到健康评估结果，输出文本里的阿拉伯数字必须能在健康评估结果中找到，中文数字先换算成数值再比对（“一次”“一项”这类量词不算数字），文本中的所有等级声明（“等级为/状态：/处于/评为”，取值域含服务实测的“良好/一般”与 SRS 的四级）都必须与评估等级一致——命中任一条不满足就退回按评分、等级与扣分指标拼出的确定性解读，并在数据说明中标注来源。`verify_report` 逐项比对报告字段与服务/图谱证据（设备 ID、评分、等级、指标、故障类型、概率、时间窗口、退化趋势、匹配故障现象、维护时机、所需资源、注意事项），状态解读另做上述可追溯校验；不通过则 `answer_verified` 为 False。
6. **发布链路**：工具返回 `report_payload(answer_source="device_prediction", report, answer_markdown, answer_verified)`；`chat_service` 新增 `_is_device_prediction_context` / `_resolve_device_prediction_output`（按 `"device-prediction" in context.skills` 判定，复用既有 `_resolve_tool_card_answer` 只发布 `answer_source="device_prediction"` 且 `answer_verified is True` 的本轮结果）。工具侧同样不返回未校验报告，两道边界各自带负例。固定文案（未配置或不可用/未找到设备/无台账 ID/服务不可用/服务不认识该设备/服务数据不完整/澄清/交互过多/校验未通过）原样透传；本轮没有任何工具结果时，按本轮原文的意图解析结果给出固定澄清句或固定失败文案，绝不回退模型自由生成的内容。

预设与技能：`agents/presets/device_prediction.py`（`slug="device-prediction-agent"`，`name="设备预测维护"`，`backend_id="ChatbotAgent"`，技能 `knowledge-base` + `device-prediction`）、`agents/skills/buildin/device-prediction/SKILL.md`（`tool_dependencies: ["predict_device"]`）。本期只交付后端接口与卡片数据契约，不新增前端卡片渲染；前端只在 `web/src/utils/messageGrouping.js` 的 `THINKING_TOOL_NAMES` 登记 `predict_device`，让思考型智能体的工具卡片与思考面板不重复（见[智能体自己拥有思考过程](./2026-10-28-agent-owned-thinking.md)）。

`kg5` 的 schema 契约（由 `contracts` 常量拥有）：

- `设备实例`：`名称`/`显示名`/`name`、`层级路径`、`电压等级`、`id`、`台账ID列表`，经 `属于` 关联 `设备类型`；
- `设备类型` -[具有现象]-> `故障现象`：`名称`、`故障码`（可多值）、`所需资源`、`注意事项`、`说明`；
- 字段取值支持数组、JSON 数组字符串与换行分隔文本；编号条目内部的分号不拆开。

## 替代方案

- **复用处置方案的 `kg4` 设备检索或 `kg3` 故障模式**：需求方明确本智能体从 `kg5` 取数；`kg4` 的语义是检修作业场景、`kg3` 是故障因果链，都没有“按预测故障类型索引维护知识”的结构。实际选择是在 `device_prediction/graph.py` 自持设备召回与维护知识查询。
- **直接复用 `maintenance_plan.graph_plan.search_devices`，只把客户端指向 `kg5`**：能省一份打分实现，但会把 `kg4` 的“必须存在 `属于` 设备类型边”口径强加给 `kg5`。实际选择是独立实现，把设备类型降级为可选加分项。
- **把图谱节点 id（`LGR#…`）直接当预测服务设备 ID**：该命名空间与业务设备 ID 不同，且 `#` 会把 URL 截断到片段。实际选择是 `台账ID列表` 优先、占位值时按精确名称回查三区台账。
- **设备节点 `台账ID列表` 为占位值时直接失败**：`kg5` 里主变1/主变2/岭开线 LN1/银开线 YN3 的台账 ID 都是“补充数据”，直接失败会让 10 台可预测设备只剩 6 台可用。实际选择是回查运维维护的三区台账；为控制误绑定风险，只接受**精确同名**行（`find_device` 的子串兜底结果会被名称复核拒绝），并用层级祖先消歧。
- **在代码里维护设备 ID 别名表**：会把设备主数据复制进代码，台账更新后仍需改代码。实际选择是只读台账对象，数据团队更新 MinIO 即生效（进程内缓存最多 5 分钟）。
- **不限定设备类型，全图匹配故障现象**：会拿变压器的“局部放电”资源去描述开关柜。实际选择是先按 `设备类型 -[具有现象]->` 收敛候选，只有设备没有类型时才退回全图。
- **省略“状态解读”**：需求表把它列为设备健康评估字段（数据来源：大模型生成），原型也展示该字段。实际选择是实现为“模型生成 + 数值可追溯校验 + 确定性兜底”。
- **把整份报告交给模型总结**：与既有智能体“证据确定性装配、发布门覆盖模型输出”的口径冲突，且四段报告全部是数值事实。实际选择是只有状态解读走模型，其余字段逐项来自服务与图谱。
- **故障码写死在代码里做别名映射**：需求方选择在 `kg5` 的故障现象上增加 `故障码` 属性。实际选择是只在代码里实现“按 `故障码` 精确匹配 + 名称匹配”的通用规则，不内置业务别名。
- **服务信封按单一形状强解析（只认顶层字段）**：两个服务的成功响应现在是顶层对象，但企业内部接口常见 `{data: {...}}` / `{result: {...}}` 包裹。实际选择是同时接受三种信封，必需字段仍按接口文档的字段名严格校验。

## 后果

- **10 台可预测设备现在都能解析**：三区台账在 2026-11-12 更新到 3173 行后，10 台服务设备 ID 全部在台账中。`kg5` 里 6 台杆塔（BN18/DN67/AN53/CN13/DN1/AN1）的 `台账ID列表` 直接给出服务 ID；主变1、主变2、岭开线 LN1、银开线 YN3 在 `kg5` 中是合成 id（`LGR#SUP…`）且 `台账ID列表` 为“补充数据”，由精确名称 + 祖先回查台账解析。这 10 台设备均已端到端跑通（`answer_verified=True`）。
- **维护计划建议只保留维护时机、所需资源与注意事项**：原「维护内容」字段已从最新需求中移除，报告、数据契约与发布内容都不再包含它，`kg5` 的 `故障现象 -[具有原因]-> 故障原因 -[具有措施]-> 处置措施` 链路也不再被本智能体读取。
- **故障码属性尚未写入 `kg5`**：当前 8 个英文故障码在图中没有对应属性，匹配靠 `predicted_fault_type` 的中文名称（实测服务返回“杆塔倾斜故障”“高温过热故障”，与现象名称一致）。`故障码` 属性补齐后，英文码与中文名称都能匹配，无需改代码。
- **开关柜分支的现象与预测标签不一致**：`kg5` 的 `开关柜` 只关联“燃烧起火”，而预测服务可能返回“开关柜绝缘退化/局放”，这类标签会标注未匹配而不是套用变压器的现象。
- **健康等级取值以服务为准**：服务实测返回“一般”“良好”，报告原样展示；SRS 里的“正常/注意/异常/严重”不是该服务的取值域，状态解读的等级声明校验同时覆盖两套取值。
- 设备缺少 `属于` 边时维护知识退回全图现象匹配，且只接受故障码/名称精确匹配；报告在数据说明中标注“设备类型缺失，按全图故障现象匹配”。
- 两个外部服务不可达或返回不完整数据时整份报告不发布，只返回对应固定文案；不会用半份数据生成报告。`asyncio.gather` 里的非服务类异常按 fail-fast 直接抛出，不伪装成服务失败文案。
- 配置项读取失败（数据库/缓存不可用）与服务地址留空都会被投影成“服务未配置”；两者在日志里分别记录真实原因。
- 状态解读多一次模型调用（`temperature 0`、关闭思考）；`DEVICE_PREDICTION_INTENT_ENABLED=false` 时意图识别与状态解读都走确定性路径。`DEVICE_PREDICTION_INTENT_TIMEOUT`、`DEVICE_PREDICTION_INTENT_MAX_TOKENS`、`DEVICE_PREDICTION_READOUT_MAX_TOKENS` 是调优开关，默认值写在 `intent.py`。`DEVICE_PREDICTION_INTENT_TIMEOUT` 同时作用于意图识别与状态解读；LangGraph 中断恢复会让工具从头重放，每次恢复随之重跑一次模型意图调用（受选择轮次预算约束）。
- 中文数字校验是“换算后比对”：`十分注意`、`三四十` 这类习惯写法可能被判为不可追溯而退回确定性解读。这是 fail-closed 取舍——宁可少用模型原文，也不发布无法追溯的数字。
- 报告不包含前端卡片；原型中的健康评分仪表盘、指标网格与预警文案属于前端渲染范围，后端只提供 `report` 数据契约与 `answer_markdown`。
- 设备可见性边界：`predict_device` 不按用户或知识库做设备可见性过滤，能使用该智能体的用户即可对 `kg5` 台账可解析的设备请求预测；授权边界当前在智能体可见性层闭合。
- 选择卡片的中断载荷解析（`_parse_choice` / `_is_other` / 设备卡片构造）与处置方案生成的实现重复。两处都是各自 Agent 的协议适配层，行为已分别被单测锁定；抽公共模块需要同时改动处置方案的已验证代码，本次按“各自拥有”处理，待第三个消费方出现时再抽。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 预设、技能与工具被平台发现并注册 | preset/skill 未被发现，或工具未注册 | `presets/device_prediction.py`、`skills/buildin/device-prediction/SKILL.md`、`toolkits/__init__.py` | `test_builtin_discovery.py`、`test_agent_repository_deep_research.py`；容器内实测 `predict_device` 注册、`device-prediction-agent` 与 `device-prediction` 落库 | 未注册时该智能体没有可调工具 | Passed |
| 服务配置项会落库、可读取 | 新增 `Option` 忘记注册进 `OPTION_DEFINITIONS`，读取即抛错并静默退化为“未配置” | `config/options.py` | `test_options.py::test_every_defined_option_is_registered`；容器内实测 `config_options` 行已创建 | 未注册时 `get()` 抛 `ValueError: 配置项不存在` | Passed |
| 设备只从 `kg5` 读取，缺设备类型层仍可召回 | 实现偷偷回退 `kg4`/`kg3`；没有 `属于` 边就召不回设备 | `device_prediction/graph.py` | `test_device_prediction_graph.py`：名称打分、类型/电压/位置加分、类型查询失败仍返回候选、只给类型时按 `属于` 召回 | 无关键词也无位置线索时不发起查询；空召回返回空列表 | Passed |
| 设备 ID 优先取 `kg5` 节点的台账 ID，占位值时按精确名称回查台账，取不到就固定文案 | 把 `LGR#…` 当业务 ID 外发；把占位值当真实 ID；台账子串匹配把报告绑定到另一台设备 | `device_prediction/graph.py` | `test_device_prediction_graph.py`：占位值跳过、精确名称 + 祖先回查、非精确同名被拒、台账不可用返回 None；真实台账 3173 行中 10/10 服务设备 ID 命中 | 设备 ID 含 `#` 时 URL 被截断；台账不可用或名称不精确时不得臆造 ID | Passed |
| 故障现象按设备类型收敛，按故障码/名称/包含/二元组匹配，并只取所需资源与注意事项 | 跨设备类型误匹配；复合故障类型漏匹配；同分依赖图返回顺序；无类型时跨类型模糊匹配；把处置措施等其他节点内容塞进维护建议 | `device_prediction/graph.py` | `test_device_prediction_graph.py`：故障码优先、名称精确、包含、复合拆分、同分取更具体名称、无类型时只接受精确匹配并标注说明、只读现象属性 | 设备类型下无匹配现象 → 固定“未匹配”说明，不返回别类现象 | Passed |
| 两个服务的错误分类（未配置/不可达/非 200/非 JSON/缺字段/404） | 失败被伪装成空结果，或缺必需字段仍产出报告 | `device_prediction/services.py` | `test_device_prediction_services.py`（20 例）：信封解析、百分比与扣分归一、数值补单位、URL 编码、必需字段逐项缺失、404 单列 | 连接异常与非 200 都不得返回半份数据 | Passed |
| 两个服务的成功响应可被正确解析 | 字段名/单位与真实响应不一致，报告出现空字段或错误数值 | `device_prediction/services.py` | 真实调用（2026-11-11）：故障预测返回 `predicted_fault_type/probability/expected_failure_time/degradation_trend/maintenance_time`；健康评估返回 `health_score/health_level/indicators/fault_rate/average_downtime_hours`，`indicator_score` 为正数扣分、`weight_ratio` 为小数权重，均被归一 | 报告字段为空或单位缺失 | Passed |
| 报告字段与服务/图谱证据逐项一致，状态解读数字可追溯 | 报告被改写、状态解读出现证据外数字（含中文数字）或与评估不一致的等级声明仍发布 | `device_prediction/answer.py` | `test_device_prediction_answer.py`（45 例）：字段篡改 15 个负例、模型解读分支、中文数字正例与量词正例、服务真实等级取值域（一般/良好）的正负例、多条等级声明、带编号条目的渲染、指标单位空格、未匹配与缺失标注、报告不再含维护内容 | 报告任一字段（含匹配故障现象）与证据不一致即 `answer_verified=False` | Passed |
| 发布门只发布本轮 `answer_source=device_prediction` 且 `answer_verified is True` 的报告 | 未校验报告、别的智能体载荷、JSON 字符串载荷、非白名单纯文本、上一轮工具结果被发布 | `chat_service._resolve_device_prediction_output` | `test_device_prediction_gate.py`（18 例，AST 提取生产源码执行，另有一条接线断言） | `answer_verified` 为 `False`/`None`/`"true"`/`1`/`0` 都拒绝；JSON 字符串载荷与未知纯文本都不发布 | Passed |
| 工具编排：设备选择中断、服务失败、图谱失败、校验失败、按问题缓存 | 候选选择丢失、失败后继续调用另一服务、同一轮重复调用外部服务、换问题命中旧缓存、图连接未关闭 | `device_prediction/tool.py` | `test_device_prediction_tool.py`（16 例）：主路径四步思考过程、图名锁定 kg5（含配置键缺失回退用例）、非服务类异常不被吞、按问题缓存、选择卡片与轮次预算、自由输入重解析、健康评估失败时故障预测标记 skipped、`kg5` 查询失败时关闭连接 | 健康评估失败仍发布只含预测的报告；`answer_verified=False` 仍返回载荷；图连接泄漏 | Passed |
| 真实 `kg5` + 真实服务 + 真实台账端到端出报告 | 设备匹配、ID 解析、服务解析、现象匹配、校验任一处不一致 | `device_prediction/tool.py` | 容器内用真实管理员图配置、服务地址与 2026-11-12 台账执行 `predict_device`：DN67 杆塔（71.2/一般，杆塔倾斜故障 86.1%）、BN18 杆塔（81/良好）、主变1（65/一般，高温过热故障 80.1%，资源 3/注意事项 4）、主变2（75.1/良好，局部放电 36.6%，资源 4/注意事项 5）、LN1 杆塔（72.6/一般）、YN3 杆塔（79.2/良好），全部 `answer_verified=True` | 任一步失败应返回固定文案而不是半份报告 | Passed |
| 知识模块可在全新解释器里单独导入 | `graph`/`tool` 与 `yuxi.agents.toolkits` 形成循环导入 | `device_prediction/tool.py` 的模块级导入 | `test_module_import_boundaries.py` 新增两个模块；容器内子进程导入通过 | 父包部分初始化时直接导入子模块失败 | Passed |
| 思考面板与工具卡片不重复 | 新智能体同时显示思考步骤与 `predict_device` 原始工具卡片 | `web/src/utils/messageGrouping.js` | `node --test test/unit/messageGrouping.test.js`（13 passed，含 `predict_device` 隐藏用例）；`eslint` 两个改动文件无告警 | 清单漏登记时卡片与思考面板同时出现 | Passed |

实际执行（容器内，`docker compose exec api`）：

```bash
uv run --no-sync --group test pytest -q -p no:cacheprovider test/unit -m 'not slow'
```

结果：**3115 passed, 54 skipped**（含本变更新增的 130 例：`test/unit/knowledge -k device_prediction` 129 例——含图名锁定 kg5 与配置键缺失回退、非服务类异常 fail-fast、合法 JSON 非对象、15 个字段篡改负例——+ 选项注册 1 例）。ruff 经 `uv run --no-sync --with ruff`（临时 0.16.4，`--cache-dir /tmp/ruff-cache`）实测 `ruff check package` → All checks passed，`ruff format package --check` → 316 files already formatted；镜像内不带常驻 ruff。`docs` 站点构建通过（`pnpm run build`）。

真实环境探测（2026-11-11 至 11-12，容器内；地址用占位符表示）：

- `kg5`：20 类节点、32 类关系、3000 台 `设备实例`；`故障现象` 11 个、`检修作业场景` 30 个；三个预测相关现象 `FP_HIGH_TEMP_OVERHEAT`/`FP_PARTIAL_DISCHARGE`/`FP_TOWER_TILT` 都有 `所需资源` 与 `注意事项`。
- 故障预测服务 `{prediction-host}:8084`：`/api/prediction/health` 200，`fault_codes` 为 8 个（`TR_PARTIAL_DISCHARGE`、`TR_LOW_ENERGY_DISCHARGE`、`TR_HIGH_ENERGY_DISCHARGE`、`TR_THERMAL_FAULT_LOW/MEDIUM/HIGH`、`TR_MIXED_DISCHARGE_THERMAL`、`TOWER_TILT`）；实测 `predicted_fault_type` 返回**中文**（“高温过热故障”“局部放电”“杆塔倾斜故障”），与 `kg5` 的故障现象名称一致。
- 设备健康评估服务 `{health-host}:8085`：Spring Boot + 达梦数据库，`/actuator/health` 200；返回 `health_score/health_level/indicators/fault_rate/average_downtime_hours`（`health_level` 取值“一般/良好”，`indicator_score` 为正数扣分、`weight_ratio` 为小数）；对数据集外设备返回 404 `{"code":"NOT_FOUND", …}`。
- 三区台账（MinIO `fault-assets/ledger/devices.json`）于 2026-11-12 更新：旧对象 13,202,863 字节 / 3033 行（备份在 `tmp/ledger_backup_devices_20261112.json`），新对象 13,283,050 字节 / 3173 行，回读 sha256 与源文件一致；`LedgerRepository`、`MaintenanceDataRepository` 均读到 3173 行，10 台可预测设备 ID 全部命中。
- 设备 ID 来源：6 台杆塔（BN18/DN67/AN53/CN13/DN1/AN1）的 `设备实例.id` 为 `LGR#<服务设备 ID>`、`台账ID列表` 首个值即服务设备 ID；主变1、主变2、岭开线 LN1、银开线 YN3 在 `kg5` 是合成 id（`LGR#SUP…`）且台账 ID 为“补充数据”，由精确名称 + 祖先回查台账解析。

未验证范围：`故障码` 属性尚未写入 `kg5`（由需求方在图中补充，届时英文码与中文名称都能匹配）；开关柜等非杆塔/主变设备的预测标签与 `kg5` 现象是否一致未联调；状态解读的模型路径未在真实 Agent 会话中跑（脚本上下文没有模型，走的是确定性解读）；网页呈现未做。
