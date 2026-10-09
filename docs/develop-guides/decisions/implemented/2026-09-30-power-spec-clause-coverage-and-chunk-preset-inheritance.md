# 电力规范条款覆盖与分块策略继承

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/power_spec/ingestion.py

三项事实由不同文件拥有：条款解析与质量门归 `backend/package/yuxi/knowledge/power_spec/ingestion.py`，条款重建钩子与任务结果归 `backend/package/yuxi/services/knowledge_task_service.py`，默认分块策略继承归 `backend/package/yuxi/knowledge/manager.py`（迁移写入在 `backend/package/yuxi/repositories/knowledge_file_repository.py`）。本记录只解释取舍，不作为运行时事实源。

## 问题

电力规范检索工具 `search_power_spec` 以文件级 `processing_params.power_spec` 为发布前置。实测知识库 `kb_frcfnr9ce6`（566 文件）三个缺陷叠加，使工具始终返回「电力规范知识库尚未完成条款级重建」：

1. **文件级分块策略永久遮蔽知识库默认值。** 上传请求里的 `chunk_preset_id` 被写进每个文件的 `processing_params`，而 `resolve_chunk_processing_params` 的优先级是 `request > file > kb`。建库后改选「电力规范条款」对已入库文件不再生效，重新入库仍按旧策略切分。
2. **重新入库路径不产生条款元数据。** `_attach_power_spec_rebuild` 只在解析路径调用；`knowledge_index`（重新入库）路径没有该钩子，所以「改策略 + 重新入库」不会写入 `processing_params.power_spec`。
3. **部分条款写法无法解析，且失败会毁掉整个文件。** `parse_power_spec_text` 在未提取到条款时抛 `ValueError("未识别到数字条款")`，而分块器 `_dispatch_markdown_parser` 不捕获，`index_file` 整体失败、文件变成 `error_indexing`。实测该库 566 个文件里 281 个（约 50%）命中此路径，其中 269 个的条款标题写在下一级标题里（`## 6.4.1.1` + `### 表17 …`）、8 个的编号重复写在正文行（`## 13.1.2` + `13.1.2 …`）。

另有一处两侧不一致：读取侧的 `is_power_spec_metadata_ready` 比写入侧的 `_quality_failure_reason` 更严（前者单独要求数字标题占比 ≤ 0.5），会出现「重建通过、检索仍判为未完成条款级重建」。

## 决策

1. **默认策略变化时迁移仍写着旧默认值的文件。** `update_database` 在知识库 `chunk_preset_id` 真正变化时，把该库中 `processing_params.chunk_preset_id` 等于旧值的文件改为新值（`KnowledgeFileRepository.migrate_processing_param`），并记录迁移文件数。文件级显式选择的其他策略不动。
2. **重新入库与解析一样重建条款元数据。** `_run_file_ids` / `_run_pending_files` 在 parse 与 index 两个分支之后统一调用 `_attach_power_spec_rebuild`；`run_knowledge_ingest` 的自动入库分支把解析阶段已算出的重建报告带回任务结果，避免结果里看不到元数据。
3. **条款解析接受多级纯编号标题。** `_CLAUSE_HEADING_PATTERN` 允许 `## 6.4.1.1` 这类只有编号的标题；标题为空时取紧邻的下一级标题作为 `clause_title`，正文从该标题之后开始。若下一行本身就是另一条条款标题，则不取它为标题（正文为空时该标题自然不产出条款）。单级纯编号标题（`## 6`）仍按章节处理，不产出条款。
4. **质量门收敛为一条规则。** `_quality_failure_reason`（写入）与 `is_power_spec_metadata_ready`（读取）共用 `_clause_quality_failure`；判据是「正文过短占比 > 0.2」，数字标题占比只用于选择更具体的拒绝原因。

## 替代方案

- **把优先级改成 `request > kb > file`**：等于取消文件级分块策略，破坏上传时按文件选策略的能力。
- **上传时若策略等于知识库默认值就不落库**：需要把「执行用参数」和「持久化参数」拆成两份，且修不了已经落库的历史文件。
- **纯编号标题的标题回退为编号**：会使 `clause_title == clause_no`，被质量门判为标题抽取失败，这些文件仍然不可用。
- **保持读取侧更严**：继续允许「重建成功但检索说未完成条款级重建」。
- **分块失败时回退 general 分块**：文件仍显示策略为 `power_spec` 而实际按 general 切分，状态不可解释；改为由调用方在失败后把文件显式改回 general。

## 后果

- 已入库文件的分块策略会跟随知识库默认值迁移；代价是无法区分「显式选了旧默认值」与「跟随旧默认值」，两者都会被迁移。
- `chunk_parser_config` 存在同类遮蔽，但它是多键字典、文件级可能只覆盖部分键，本记录不处理。
- 无标题条款用编号作标题，标题质量改由「正文是否充分」把关；标题只剩编号且正文过短的伪条款仍被拒绝，`未识别到数字条款` 的失败面保留。
- 条款覆盖仍有上限：附录式编号（`D.3.2`、`E.1`）与标题为「表N」的纯表格片段不产出条款，需按 general 分块。实测该库 20 个文件属于此类。
- 「默认值更新」与「文件迁移」分属两个事务：迁移失败会留下「默认值已改、文件仍旧值」的中间态。因迁移以文件当前值为准、可整体重跑收敛，影响有限；不要把这两步包进同一个事务（迁移需要独立会话批量提交）。
- 重新引入条件：出现需要「文件级策略独立于知识库默认值且不随默认值迁移」的真实消费者时，改为记录策略来源而不是按值迁移。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 改默认策略后仍跟随默认的文件一起变更，显式覆盖的不变 | 旧值遮蔽新默认值 | `knowledge/manager.py` | 真实服务 + 真实库：`update_database` 后读回两个文件（一个 `general`、一个 `qa`），日志 `migrated 1 inheriting files` | 显式 `qa` 的文件保持 `qa` | 通过 |
| 重新入库重建条款元数据 | 入库路径缺钩子 | `services/knowledge_task_service.py` | `pytest test/unit/services/test_knowledge_task_power_spec_source.py` | `test_run_knowledge_index_rebuilds_power_spec_metadata` 断言 index 后调用钩子 | 通过 |
| 多级纯编号标题产出条款且标题取自下级标题 | 整文件 `error_indexing` | `knowledge/power_spec/ingestion.py` | `pytest test/unit/knowledge/test_power_spec_standard_markdown.py`；对 566 个真实文件 dry-run：`accepted=546 rejected=20` | `test_number_only_single_level_heading_is_not_a_clause` 断言 `## 6` 仍抛 `未识别到数字条款` | 通过 |
| 无标题但正文充分的条款在写入与读取两侧都放行 | 重建通过但检索拒绝 | `knowledge/power_spec/ingestion.py` | `pytest test/unit/knowledge/test_power_spec_rebuild.py` | `test_power_spec_metadata_ready_rejects_heading_only_extraction` 断言编号标题 + 短正文仍被拒 | 通过 |
| 工具不再返回「尚未完成条款级重建」 | 检索前置门全量过滤 | `knowledge/power_spec/tool.py`（消费读取侧规则） | 真实 Milvus + 重排：`ready_files=546`，两次提问均 `raw=20 filtered=20`，`verify_evidence_projection` `valid=True` | 修复前同一链路 `filtered=0` | 通过 |

数据修复结果（`kb_frcfnr9ce6`）：566 个文件全部 `indexed`，546 个为 `power_spec` 且带 `processing_params.power_spec`，20 个按 `general` 保留可用；无 `error_indexing`。

回归：`pytest test/unit/knowledge test/unit/plugins test/unit/routers/test_knowledge_router_cleanup.py test/unit/services` → `1210 passed`。

工程门（宿主 python 只有 3.7.9，用 api 镜像挂载仓库根执行）：`python scripts/verify_engineering_contracts.py` → 通过（114 decisions / 5 workflows / 4 agents files / 165 docs / 27 routers / 250 web sources）；`python -m unittest scripts.test_verify_engineering_contracts` → 62 passed。

未执行：`backend/test/integration/api/test_knowledge_router.py` 的新增用例 `test_update_database_migrates_files_inheriting_chunk_preset` 需要 `backend/test/.env.test` 的 `TEST_USERNAME`/`TEST_PASSWORD`，本机未配置（默认整批 skip）；docs build 需要 pnpm/node，本机未安装。
