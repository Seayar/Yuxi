# 处置方案分支不再人工选择，并修正设备意图抽取

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/maintenance_plan/tool.py

## 问题

测试反馈两个缺陷：生成处置方案时仍会弹出分支选择卡片，要求人工在分支1/分支2之间选择；部分运维任务描述不能被意图识别为具体设备，导致工具返回澄清文案或按设备类型召回大量候选。

分支数据只在 Redis 中，键为 `{台账ID}:{分支判定维度}`，值是场景的 `分支1条件` 或 `分支2条件`。2026-11-16 只读核对 kg5 与 Redis：Redis 有 30 个分支 key，对应 27 个台账 ID，其中 21 个能在图谱设备 `台账ID列表` 中直接命中，另外 6 个需要经 MinIO 台账按名称+层级补全；30 个 key 是按场景样例设备录入的，不是按设备类型覆盖全部 3002 个设备实例。设备缺少可选分支 key 或选中的场景维度没有对应 key 时，旧实现会发 `interrupt` 让用户确认分支。这个行为在数据未覆盖的设备上必然出现，不能靠前端隐藏解决。

设备意图抽取旧实现按 `_DEVICE_NAME_PATTERNS` 的顺序取第一个命中，会把 `3B-6K线99#杆避雷器` 截成线路名 `3B-6K`、把 `#1主变高压侧电流互感器` 截成 `#1主变`；`991SVG柜`、`柴发PT柜`、`910-1隔离柜`、`BN91A相绝缘子串`、`971电容器组` 等台账名称没有对应规则。模型调用超时或返回非法 JSON 时回退规则解析，这些输入就丢失了设备。

## 决策

1. **分支只读 Redis，不再人工选择**：`tool._resolve_branch` 只在 `lookup_branch` 返回 `ok` 时使用 Redis 值；`not_required` 按无分支处理；`not_configured / missing / unavailable / invalid` 一律返回固定文案 `BRANCH_UNRESOLVED_MESSAGE`（“未读取到该设备的检修分支判定值，无法生成标准化处置方案”）。不默认分支1，也不生成缺少分支相关安全措施/资源/人员的残缺方案。该文案加入 `PLAIN_TOOL_MESSAGES`，由发布门原样透传。
2. **删除分支中断路径**：移除 `BRANCH_CHOICE_SOURCE`、`BRANCH_QUESTION_ID`、`BRANCH_CHOICE_QUESTION`、`_branch_payload`、`_branch_from_answer` 和 `BRANCH_USER_CONFIRMED_NOTE`；`interrupt` 只保留设备/场景两个引导选择点。
3. **分支 key 允许维度别名**：`branch.lookup_branch` 先查 `{台账ID}:{分支判定维度}`；没有命中时扫描该设备的其他中文维度 key，按**值**匹配场景的 `分支1条件`/`分支2条件`。数据侧把维度写成 `带电范围`、场景写 `停电范围` 时仍能自动判定；英文实时属性不参与扫描。多个 key 给出不同分支或值不匹配时仍判 `invalid`。
4. **设备意图取最长命中**：`intent._match_device_name` 收集全部规则命中并按去空白后的长度取最长；新增 `991SVG柜`、`柴发PT柜`、`910-1隔离柜`、`3B-6K线99#杆避雷器`、`BN91A相绝缘子串`、`#1主变高压侧电流互感器`、`971电容器组` 等台账写法的规则。
5. **图谱按用户原文全名重排**：`graph_plan._build_candidate` 在设备名（NFKC、去空白、忽略大小写）完整出现在 `query.raw_text` 时取 `max(原名称分, TEXT_NAME_SCORE + min(len(name), 30) * 10)`，让 `3B-6K线99#杆避雷器` 优先于 `3B-6K`、`#1主变高压侧电流互感器` 优先于 `#1主变`。文本命中不叠加原名称分，否则短名的“精确相等”分会压过完整名称。该打分只影响同一批召回候选的排序，不改变“只保留有 `属于` 类型边设备”的过滤。

## 替代方案

- **保留分支选择卡片**：测试明确不允许人工选择分支；且卡片不能区分“数据缺失”和“用户需要决策”，会把数据缺口变成人工操作。未采用。
- **分支缺失时默认分支1**：会把错误分支的安全措施、资源和人员发布到现场。2026-11-05 记录已拒绝该方案；本次仍拒绝。
- **分支缺失时只生成通用段**：步骤与分支无关，但安全措施、资源、人员会缺少当前分支内容，`verify_report` 也无法证明完整性。未采用。
- **在代码里内置分支属性映射**：分支值是设备实例属性，不是设备类型属性；内置映射会引入第二套事实源，且没有数据维护方。未采用。
- **只改提示词让模型识别设备**：模型超时或返回非法 JSON 时仍会回退规则；提示词不能解决规则漏名。未采用。
- **为所有设备补 Redis 分支 key**：这是彻底消除 `BRANCH_UNRESOLVED_MESSAGE` 的数据方案，但当前 30 个 key 只覆盖样例设备；本次代码先保证不再弹人工选择，数据覆盖由数据团队按设备台账补齐。

## 后果

- 有分支 key 的设备正常生成方案；没有 key 的设备返回固定失败文案，不再弹出分支选择卡片。测试需要按具体设备核对 Redis key，而不是只看场景是否有分支条件。
- 设备意图规则变长，必须保持“具体模式在前、最长命中兜底”的顺序；新增台账名称写法时优先补规则，不靠模型自由发挥。
- `TEXT_NAME_SCORE` 只用于同一批召回候选的重排；如果用户原文包含一个与目标设备无关的完整设备名，候选排序可能偏向该名称。现有 `_select_items` 与证据校验仍会阻止编造内容，测试需覆盖“原文含多个设备名”的负向案例。
- `MAX_CHOICE_ROUNDS` 仍约束设备/场景选择轮次，不再被分支选择消耗。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 分支缺失不再发 `interrupt` | 仍弹分支选择卡片 | `maintenance_plan/tool.py`、`contracts.py` | `test_branch_missing_returns_fixed_message_without_interrupt` 断言 `_resolve_branch` 返回固定文案且 `interrupt` 未被调用 | 旧测试恢复分支选择路径会重新出现弹窗 | 单测已改，待容器执行 |
| 维度别名按值匹配 | `带电范围` key 在场景 `停电范围` 下被当作缺失 | `maintenance_plan/branch.py` | `test_dimension_alias_uses_value_match`：精确 key 缺失，扫描到 `母线带电`，返回 `ok/分支2` | 值不匹配或两个 ID 给出不同分支时判 `invalid` | 单测已改，待容器执行 |
| 分支固定文案可被发布门透传 | 文案被替换为失败/模型文本 | `chat_service._resolve_maintenance_plan_output`、`PLAIN_TOOL_MESSAGES` | 固定文案加入白名单；发布门按白名单原样返回 | 文案不在白名单时返回 `PLAN_VERIFICATION_FAILED_MESSAGE` | 待单测执行 |
| `3B-6K线99#杆避雷器` 不被截成 `3B-6K` | 设备名截断导致选错设备 | `maintenance_plan/intent.py` | 规则探针：输入上述原文返回完整设备名与类型 `避雷器` | 只命中短模式时返回 `3B-6K` | 已用真实规则函数实测 |
| `991SVG柜`、`柴发PT柜`、`910-1隔离柜`、`BN91A相绝缘子串`、`971电容器组` 可抽设备 | 规则回退时设备为空 | `maintenance_plan/intent.py` | 规则探针对上述输入均返回完整设备名 | 旧规则返回 `None` | 已用真实规则函数实测 |
| 用户原文全名优先于短名 | 选错设备 | `maintenance_plan/graph_plan.py` | `TEXT_NAME_SCORE` 让完整名称候选在召回集中排前；待补图检索单测 | 短名候选仍排第一 | 待单测执行 |

未验证范围：本次没有可用的 Docker/PostgreSQL/真实 LangGraph 会话，未执行 `pytest`、真实 HTTP 中断链路和前端弹窗验证；规则探针只覆盖纯函数。分支 key 覆盖仍受 Redis 数据限制，未替数据团队补写。

已执行：`python` 内存编译 13 个改动文件通过；真实 `rule_extract` 探针对 7 个新增台账名称均返回完整设备名；分支维度别名与候选排序用假 Redis/假候选纯函数探针验证通过；`python scripts/verify_engineering_contracts.py` 通过（146 decisions / 199 docs）。
