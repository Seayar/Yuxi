# 电力规范卡片字段收敛为四项

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/power_spec/answer.py

## 问题

最新需求规格说明书规定，规范卡片返回的字段只有规范名称、标准编号、条款编号和条款正文。该版本由需求方在仓库外提供，截至本记录未纳入版本控制；仓库内 `srs/电力规范检索-需求规格说明书.md` 仍是旧版 11 字段，不能作为当前契约的验收依据。现有链路仍按旧版 4.1 渲染十一个字段，`_format_item` 还在 `spec_results` 中附带版本、源文件、命中词和引用键等派生键；`render_srs_cards` 会渲染标题、适用范围、状态、替代规范、相关度、页码、位置与引用标签。整理子智能体也被要求输出 `clause_title` 和 `scope`，模型可见输出与最新卡片契约不一致。

## 决策

1. `SRS_CARD_FIELDS` 只保留规范名称、标准编号、条款编号、条款正文四项，顺序与最新需求书一致；`_format_item` 返回的字典只含这四个键，`answer_markdown` 每张卡片只渲染这四个字段。
2. 卡片不再渲染相关度、引用标记或来源页码。条款标题、适用范围、规范状态、替代关系、页码和引用键仍保留在 `SpecClauseEvidence` 中，继续服务召回排序、整理子智能体的输入和 `verify_evidence_projection`，但不进入卡片载荷。
3. 整理与最终合并的结构化输出收敛为 `citation_key`、`clause_no`、`body`；标题与适用范围不再要求模型生成，也不再从模型结果回填卡片。
4. 上游 Web 的规范卡片组件只展示规范名称、标准编号、条款编号和正文，删除标题、适用范围、状态、替代规范、相关度、页码、位置和引用展示。

## 替代方案

1. **保留完整 `spec_results`、只在 UI 隐藏多余字段**：字段仍会出现在工具结果和 SSE 载荷里，消费方无法根据契约判断哪些是卡片字段，不满足“返回字段仅有四项”。
2. **把标题并入条款正文**：正文与标题的来源和校验规则不同，合并后无法再区分“原文正文”与“派生标题”，也违反最新字段定义。
3. **只改 Markdown、不改 `spec_results`**：结构化工具结果与展示文本会形成两套字段集，前端或后续消费者仍会读到已删除的字段。

## 后果

- 卡片载荷和 Markdown 的字段集合固定为四项；依赖 `clause_title`、`scope`、`relevance`、`source_page` 等键的调用方需要改为从内部证据或独立接口获取。
- 检索、排序、投影校验和条款字段校对不受影响，因为它们读取的是 `SpecClauseEvidence`，不是卡片字典。
- 模型输出更短，但卡片不再显示条款标题、适用范围、规范状态、替代关系和页码。最新需求接受这一信息损失。
- 前端组件删除对应展示与样式；没有数据库 schema 变更，历史入库数据无需迁移。
- `SpecClauseEvidence` 的标题、适用范围、状态、替代关系、页码和引用键保持不变，后续恢复全部字段时只需重建展示与发布层，不需要改回解析、校对和检索；被删除的展示代码可以从版本历史恢复。

## 验证

| 主张 | 证据 |
| --- | --- |
| 卡片载荷只有四个键 | 单测 `test_format_power_spec_results_returns_only_four_card_fields` 断言 `set(item)` 等于四个字段 |
| Markdown 不出现已删除字段 | 单测 `test_render_srs_cards_omits_fields_outside_the_latest_contract` 断言标题、适用范围、状态、替代规范、相关度、页码、位置和引用标签都不存在 |
| 模型整理结果只回填编号与正文 | 单测 `test_apply_organized_clauses_uses_model_values_but_keeps_code_format`、`test_parse_organized_draft_accepts_clause_no` |
| 工具返回的 `spec_results` 同样收敛 | `test_confirmed_intent_path_returns_organized_answer` 断言 `spec_results[0]` 的键集合 |
| 旧字段不残留 | `rg` 检查 `answer.py` 的 `_format_item` 与 `SRS_CARD_FIELDS`；前端组件不再引用 `clause_title`、`scope`、`relevance`、`source_page`、`citation_key` |

实际执行的命令与结果：

- `docker compose exec -T api uv run --no-sync --group test pytest test/unit/knowledge -k power_spec -q -p no:cacheprovider`：170 passed。
- `docker compose exec -T api uv run --no-sync --group test pytest test/unit -m "not slow" -q -p no:cacheprovider`：2928 passed，54 skipped。
- `docker compose exec -T web sh -c 'npx eslint . --max-warnings=0'`：通过。
- `docker compose exec -T web sh -c 'pnpm run build'`：通过。
- `docker compose exec -T web sh -c 'node --test test/unit/toolRegistry.test.js test/unit/toolRendering.test.js'`：18 passed。全量 `pnpm run test:unit` 未跑完，容器内相关子集即耗时约两分钟，全量超时。
- `cd docs && pnpm run build`：通过。
- `py -3.12 scripts/verify_engineering_contracts.py`：通过（129 decisions / 5 workflows / 4 agents files / 182 docs / 28 routers / 250 web sources）。
- `uvx ruff@0.16.4 check --no-cache package`：通过；`uvx ruff@0.16.4 format --check` 对改动文件通过，仓库仍有 4 个与本改动无关的历史文件待格式化。
- 进程内样例：`format_power_spec_results` 返回的 `spec_results[0]` 键为 `clause_body`、`clause_no`、`standard_name`、`standard_no`；`answer_markdown` 只含规范名称、标准编号、条款编号和条款正文。

未验证或环境限制：

- Windows 上 `python -m unittest scripts.test_verify_engineering_contracts` 的 62 个用例中 61 个通过，`test_decision_owner_symlink_cannot_escape_repository` 因当前账户无创建符号链接权限而报 `WinError 1314`；api 容器未挂载 `scripts/`，无法在 Linux 容器内重跑。
- 未启动真实前端页面复核卡片视觉效果；未在真实知识库上重跑完整检索链路。前端构建与组件 lint 只证明代码可编译、格式合法。
