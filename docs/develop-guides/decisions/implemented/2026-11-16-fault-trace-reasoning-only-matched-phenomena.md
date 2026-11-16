# 故障溯源推理过程只展示命中现象

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/fault_trace/workflow.py

> 本记录更新[故障溯源与处置方案统一读取 kg5](./2026-11-13-unify-knowledge-graph-kg5.md)第 3 条的 ② 展示表口径；定位收口逻辑不变。

## 问题

故障溯源报告 ② 推理过程按设备类型列出该类型在图谱里的全部故障现象。设备类型挂多个现象时（例如架空线路同时挂“导线断股”和“异物搭挂”），即使图片只识别到其中一种，表格仍展示整张现象目录，读者无法从表格判断本次故障现象。③ 原因定位已经只使用命中现象可达的原因，② 与 ③ 的口径不一致。

## 决策

现象匹配以图片识别类别为准：`_match_phenomena` 在存在 `image_fault_name` 时只用它匹配图谱现象，图片类别没有对应节点时才退回文字现象。命中现象集合同时驱动 ② 表格与 ③ 定位范围；① 的结构化 `phenomenon` 字段取该集合（没有命中时回退推理表第一行），摘要自由文本仍由模型按用户描述与图片识别结果生成。

推理表只对命中现象成行：`_reasoning_rows` 接收“命中现象，否则全部现象”的展示集合。一个现象都没有命中时不输出空表，保留该设备类型的全部现象并沿用“未匹配到用户描述中的故障现象，推理过程列出该设备类型的全部现象”告警，③ 继续按成立原因反推现象。

思考步骤 `match_causal_chain` 的 `phenomena` 仍保留图谱全量证据与 `matched` 标记；表格收敛只作用于报告，不改变图谱读取范围。

## 替代方案

- **只在 `answer.py` 渲染层过滤**：判断“哪些现象属于本次故障”需要命中集合，渲染层拿不到它，只能再算一套匹配；放弃。
- **文字与图片现象取并集**：与“只保留图片识别到的现象”的要求冲突，图片与文字不一致时表格仍会多出现象；放弃。
- **命中为空时输出空表**：② 失去排查入口，③ 的“按成立原因反推现象”回退也失去数据来源；放弃。
- **同时修改图谱/实时值读取**：本次只收敛展示与定位口径，图谱查询、Redis 读取与发布门保持不变。

## 后果

- 图片类别命中时，推理表只出现该现象；文字描述但没有被图片确认的现象不再进入 ② 与 ③。
- 图片类别在图谱中没有对应现象时退回文字现象；两者都没有命中时保持旧行为（全部现象 + 告警），报告仍按确定性内容发布。
- 设备类型解析错误时表现为“全部现象 + 未匹配告警”，不会伪造命中现象。
- 报告结构与 `fault-trace-report/v1` 不变，`reasoning` 数组的行数可能从“设备类型现象数”降为“命中现象数”。

## 验证

- `test/unit/knowledge/test_fault_trace_tool.py`：新增 `test_trace_fault_reasoning_keeps_only_yolo_phenomenon`（图片命中一个现象时推理表只有一行）、`test_trace_fault_reasoning_prefers_yolo_over_text`（文字与图片不一致时跟随图片）；更新 `test_trace_fault_scopes_location_to_matched_phenomenon`（推理表与 Markdown 不再出现未命中的“冒烟异常”）和 `test_trace_fault_falls_back_to_phenomena_of_located_causes_when_no_match`（无命中时保留全部现象）。
- 命令与结果：`pytest test/unit/knowledge/test_fault_trace_tool.py` 28 passed；`pytest test/unit/knowledge/test_fault_trace_answer.py test_fault_trace_gate.py test_fault_trace_intent.py test_fault_trace_confirmation_replay.py test_fault_trace_graph_kg3.py test_fault_trace_graph.py test_fault_trace_ledger.py test_fault_trace_realtime.py test_fault_trace_report_text.py test_fault_trace_rules.py test_fault_trace_images.py test_chat_service_fault_trace_scope.py test_thinking_fixture_contract.py` 100 passed；`ruff check` 与 `ruff format --check` 通过；`python scripts/verify_engineering_contracts.py` 通过（146 decisions），`python -m unittest scripts.test_verify_engineering_contracts` 62 passed；`docs` 构建通过。
- 范围回归：`pytest test/unit/knowledge --ignore=test/unit/knowledge/eval` 837 passed、6 failed。6 个失败与本改动面无关：`test_power_spec_formatting.py` 4 个来自工作区既有的 PDF 标记改动，`test_device_prediction_intent.py` 1 个来自工作区既有的设备名解析问题，`test_parser_capabilities.py` 1 个来自本地 FastAPI 版本不支持 `Query(enum=...)`。`test/unit/knowledge/eval` 在本地依赖组合下收集失败，未运行。
- 执行环境：WSL Ubuntu-22.04 + uv 安装的 Python 3.12（本机 Docker 未启动，未使用 compose 运行）；真实 TuGraph、Redis、MinIO 未参与本次验证，改动不触及这些读取路径。
